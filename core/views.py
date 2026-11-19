import base64
import binascii
import json
import os

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.exceptions import ValidationError
from PIL import Image
from datetime import datetime, timezone
from uuid import uuid4

from django.shortcuts import render
from django.http import JsonResponse
from django.conf import settings
from django.contrib.auth import authenticate, login as django_login
from django.contrib.auth.hashers import check_password, make_password
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

def _save_profile_picture(value):
    if not value:
        return ''
    if not isinstance(value, str):
        raise ValidationError('Invalid profile picture.')

    # If it's a data URI (compressed image from the client canvas compressor),
    # validate it and store it directly in MongoDB!
    # On cloud platforms like Render (free tier), the local container filesystem is
    # ephemeral and gets completely wiped when the server spins down or restarts.
    # Storing the lightweight compressed image (~30 KB) directly in MongoDB guarantees
    # it is permanent and will NEVER disappear on logout, spin-down, or device switch!
    if value.startswith('data:'):
        header, separator, encoded = value.partition(',')
        image_types = {
            'data:image/jpeg;base64': ('jpeg', 'jpg'),
            'data:image/png;base64': ('png', 'png'),
            'data:image/webp;base64': ('webp', 'webp'),
        }
        image_type = image_types.get(header)
        if not separator or not image_type or len(encoded) > 2 * 1024 * 1024:
            raise ValidationError('Only JPG, PNG and WebP images up to 1.5 MB are allowed.')
        try:
            image_data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError('The uploaded image is invalid.')
        if len(image_data) > 1.5 * 1024 * 1024:
            raise ValidationError('Profile picture must not exceed 1.5 MB.')

        try:
            picture = Image.open(ContentFile(image_data))
            picture.verify()
        except Exception:
            raise ValidationError('The uploaded image is invalid.')

        return value

    if value.startswith(('http://', 'https://')):
        return value
    if value.startswith(settings.MEDIA_URL):
        return value
    return value


def _profile_picture_url(value):
    if not value:
        return ''
    if value.startswith(('data:', 'http://', 'https://')):
        return value
    if value.startswith(settings.MEDIA_URL):
        return value
    # If it's a stored relative path and exists on disk, serve via storage url
    try:
        if default_storage.exists(value):
            return default_storage.url(value)
    except Exception:
        pass
    return value


def _migrate_legacy_profile_picture(db, user):
    return user.get('profile_pic') or user.get('profilePic', '')

def _mongo():
    return MongoClient(settings.DATABASES['default']['HOST'], serverSelectionTimeoutMS=7000)


def _db(client):
    return client[settings.DATABASES['default']['NAME']]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _role_collection(db, role):
    return db['farmers' if role == 'farmer' else 'buyers']


def _find_user(db, user_id=None, username=None):
    """Read current accounts from the legacy users collection and existing role collections."""
    collections = ('users', 'farmers', 'buyers')
    for name in collections:
        query = {}
        if user_id is not None:
            query = {'id': user_id}
        elif username is not None:
            query = {'username': username}
        else:
            continue
        found = db[name].find_one(query)
        if not found and user_id is not None:
            try:
                from bson import ObjectId
                found = db[name].find_one({'_id': ObjectId(user_id)})
            except Exception:
                found = None
        if found:
            if not found.get('role'):
                found['role'] = 'farmer' if name == 'farmers' else ('buyer' if name == 'buyers' else '')
            return found
    return None


def _user_id(user):
    return str(user.get('id') or user.get('_id', ''))


def _public_user(user):
    return {
        'id': _user_id(user),
        'username': user.get('username', ''),
        'fullName': user.get('full_name') or user.get('fullName') or user.get('name', ''),
        'email': user.get('email', ''),
        'role': user.get('role', ''),
        'phone': user.get('phone', ''),
        'district': user.get('district', ''),
        'profilePic': _profile_picture_url(user.get('profile_pic') or user.get('profilePic', '')),
        'status': user.get('status', 'active'),
        'securityQuestion': user.get('security_question', ''),
        'createdAt': user.get('created_at') or user.get('registered_on', ''),
        'cooperative': user.get('cooperative', ''),
        'cropsGrown': user.get('crops_grown', []),
    }


def _current_user(db, request):
    uid = request.session.get('user_id')
    if not uid:
        return None
    if request.session.get('user_role') == 'admin':
        return {
            'id': uid, 'role': 'admin', 'status': 'active',
            'full_name': request.session.get('user_name', 'Administrator'),
            'username': request.session.get('username', ''),
        }
    return _find_user(db, user_id=uid)


def _collection(db, primary, legacy=None):
    # Keep using the project's existing collection names; read legacy aliases if present.
    if primary in db.list_collection_names():
        return db[primary]
    if legacy and legacy in db.list_collection_names():
        return db[legacy]
    return db[primary]


def _record_selector(collection, record_id):
    found = collection.find_one({'id': record_id})
    if found:
        return {'id': record_id}
    try:
        from bson import ObjectId
        return {'_id': ObjectId(record_id)}
    except Exception:
        return {'id': record_id}


def _public_product(product):
    return {
        'id': str(product.get('id') or product.get('_id', '')),
        'farmerId': str(product.get('farmer_id') or product.get('farmerId', '')),
        'farmerName': product.get('farmer_name') or product.get('farmerName', ''),
        'name': product.get('name') or product.get('product_name', ''),
        'category': product.get('category', ''),
        'quantity': product.get('quantity', product.get('available_quantity', 0)),
        'unit': product.get('unit', ''),
        'price': product.get('price', product.get('unit_price', 0)),
        'district': product.get('district', ''),
        'description': product.get('description', ''),
        'image': product.get('image') or product.get('image_url', ''),
        'status': product.get('status', 'Approved'),
        'createdAt': product.get('created_at') or product.get('created_on', ''),
    }


def _public_order(order):
    return {
        'id': str(order.get('id') or order.get('_id', '')),
        'buyerId': str(order.get('buyer_id') or order.get('buyerId', '')),
        'buyerName': order.get('buyer_name') or order.get('buyerName', ''),
        'productId': str(order.get('product_id') or order.get('productId', '')),
        'productName': order.get('product_name') or order.get('productName', ''),
        'farmerId': str(order.get('farmer_id') or order.get('farmerId', '')),
        'farmerName': order.get('farmer_name') or order.get('farmerName', ''),
        'quantity': order.get('quantity', 0),
        'unit': order.get('unit', ''),
        'amount': order.get('amount', order.get('total_amount', 0)),
        'status': order.get('status', 'Pending'),
        'createdAt': order.get('created_at') or order.get('order_date', ''),
    }


# Page views
def home(request):
    return render(request, 'index.html')


def products_page(request):
    return render(request, 'products.html')


def market_prices_page(request):
    return render(request, 'pages/market-prices.html')


def about_page(request):
    return render(request, 'pages/about.html')


def _protected_page(request, role, template):
    if not request.session.get('user_id') or request.session.get('user_role') != role:
        from django.shortcuts import redirect
        return redirect('login')
    server_user = {
        'id': request.session.get('user_id', ''),
        'username': request.session.get('username', ''),
        'fullName': request.session.get('user_name', ''),
        'role': role,
    }
    if role in ('farmer', 'buyer'):
        client = None
        try:
            client = _mongo()
            db = _db(client)
            user = _find_user(db, user_id=server_user['id'])
            if user:
                profile_pic = _migrate_legacy_profile_picture(db, user)
                server_user.update({
                    'username': user.get('username', server_user['username']),
                    'fullName': user.get('full_name') or user.get('fullName') or server_user['fullName'],
                    'email': user.get('email', ''),
                    'phone': user.get('phone', ''),
                    'district': user.get('district', ''),
                    'profilePic': _profile_picture_url(profile_pic),
                    'createdAt': user.get('created_at') or user.get('registered_on', ''),
                })
        except Exception:
            pass
        finally:
            if client:
                client.close()
    return render(request, template, {'server_user': server_user})


def admin_dashboard(request):
    return _protected_page(request, 'admin', 'admin-dashboard.html')


def farmer_dashboard(request):
    return _protected_page(request, 'farmer', 'farmer-dashboard.html')


def buyer_dashboard(request):
    return _protected_page(request, 'buyer', 'buyer-dashboard.html')


def legacy_dashboard(request):
    if not request.session.get('user_id'):
        from django.shortcuts import redirect
        return redirect('login')
    return render(request, 'Dashbord.html')


def login_page(request):
    return render(request, 'login.html')


@csrf_protect
def api_login(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    try:
        data = json.loads(request.body or '{}')
        raw_username = str(data.get('username', '')).strip()
        username = raw_username.lower()
        password = data.get('password', '')
        role = data.get('role', '')
        if not username or not password or role not in ('admin', 'farmer', 'buyer'):
            return JsonResponse({'success': False, 'message': 'Enter your username, password, and account type.'}, status=400)

        # The existing Django superuser is authenticated through Django's auth_user collection.
        if role == 'admin':
            from django.contrib.auth import get_user_model
            UserModel = get_user_model()
            admin_record = UserModel.objects.filter(username__iexact=raw_username).first()
            admin_user = authenticate(request, username=admin_record.get_username(), password=password) if admin_record else None
            if not admin_user or not (admin_user.is_staff or admin_user.is_superuser) or not admin_user.is_active:
                return JsonResponse({'success': False, 'message': 'Invalid administrator credentials.'}, status=401)
            django_login(request, admin_user)
            uid = 'django:' + str(admin_user.pk)
            public = {
                'id': uid, 'username': admin_user.get_username(),
                'fullName': admin_user.get_full_name() or admin_user.get_username(),
                'email': admin_user.email, 'role': 'admin', 'phone': '',
                'district': '', 'profilePic': '', 'status': 'active',
                'securityQuestion': '', 'createdAt': admin_user.date_joined.isoformat() if admin_user.date_joined else '',
            }
            request.session['user_id'] = uid
            request.session['user_role'] = 'admin'
            request.session['user_name'] = public['fullName']
            request.session['username'] = public['username']
            return JsonResponse({'success': True, 'user': public, 'role': 'admin'})

        client = _mongo()
        try:
            db = _db(client)
            user = _find_user(db, username=username)
            if not user or user.get('role') != role or not check_password(password, user.get('password_hash', '')):
                return JsonResponse({'success': False, 'message': 'Invalid username or password.'}, status=401)
            if user.get('status', 'active').lower() not in ('active', 'approved'):
                return JsonResponse({'success': False, 'message': 'This account is not active.'}, status=403)
            request.session['user_id'] = _user_id(user)
            request.session['user_role'] = role
            request.session['username'] = user.get('username', '')
            request.session['user_name'] = user.get('full_name', '')
            _migrate_legacy_profile_picture(db, user)
            return JsonResponse({'success': True, 'user': _public_user(user), 'role': role})
        finally:
            client.close()
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid login information.'}, status=400)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Login service is temporarily unavailable.'}, status=503)


@csrf_protect
def api_logout(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    request.session.flush()
    return JsonResponse({'success': True})


@csrf_protect
def api_password_reset(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    try:
        data = json.loads(request.body or '{}')
        username = str(data.get('username', '')).strip().lower()
        action = data.get('action')
        client = _mongo()
        try:
            db = _db(client)
            if action == 'question':
                user = _find_user(db, username=username)
                if not user or not user.get('security_question'):
                    return JsonResponse({'success': False, 'message': 'No recovery question is available for that account.'}, status=404)
                request.session['password_reset_username'] = username
                return JsonResponse({'success': True, 'question': user['security_question']})
            if action == 'verify':
                user = _find_user(db, username=username)
                if not user or not check_password(str(data.get('answer', '')).strip().lower(), user.get('security_answer_hash', '')):
                    return JsonResponse({'success': False, 'message': 'Incorrect recovery answer.'}, status=400)
                request.session['password_reset_user_id'] = _user_id(user)
                request.session['password_reset_role'] = user.get('role')
                return JsonResponse({'success': True})
            if action == 'reset':
                user_id = request.session.pop('password_reset_user_id', None)
                role = request.session.pop('password_reset_role', None)
                password = data.get('password', '')
                if not user_id or len(password) < 6:
                    return JsonResponse({'success': False, 'message': 'Recovery expired or password is too short.'}, status=400)
                target = db['farmers' if role == 'farmer' else 'buyers']
                result = target.update_one({'id': user_id}, {'$set': {'password_hash': make_password(password)}})
                if result.matched_count == 0:
                    result = db['users'].update_one({'id': user_id}, {'$set': {'password_hash': make_password(password)}})
                if result.matched_count != 1:
                    return JsonResponse({'success': False, 'message': 'Account not found.'}, status=404)
                return JsonResponse({'success': True})
            return JsonResponse({'success': False, 'message': 'Invalid recovery action.'}, status=400)
        finally:
            client.close()
    except (ValueError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid recovery information.'}, status=400)


@csrf_protect
def api_products(request):
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    client = _mongo()
    try:
        db = _db(client)
        user = _current_user(db, request)
        if not user:
            return JsonResponse({'success': False, 'message': 'Please sign in.'}, status=401)
        products = _collection(db, 'produce_listings', 'products')
        if request.method == 'GET':
            role = user.get('role')
            query = {} if role == 'admin' else ({'farmer_id': _user_id(user)} if role == 'farmer' else {'status': 'Approved'})
            docs = [_public_product(p) for p in products.find(query).sort('_id', -1)]
            # Existing legacy listings without the newer status field remain visible to buyers.
            if role == 'buyer':
                docs += [_public_product(p) for p in products.find({'status': {'$exists': False}})]
            return JsonResponse({'success': True, 'products': docs})
        if user.get('role') != 'farmer':
            return JsonResponse({'success': False, 'message': 'Only farmers can add products.'}, status=403)
        data = json.loads(request.body or '{}')
        name = str(data.get('name', '')).strip()
        category = str(data.get('category', '')).strip()
        unit = str(data.get('unit', '')).strip()
        district = str(data.get('district', user.get('district', ''))).strip()
        quantity, price = float(data.get('quantity', 0)), float(data.get('price', 0))
        if not name or not category or not unit or quantity <= 0 or price <= 0:
            return JsonResponse({'success': False, 'message': 'Enter a product name, category, unit, valid quantity and price.'}, status=400)
        product = {
            'id': str(uuid4()), 'farmer_id': _user_id(user),
            'farmer_name': user.get('full_name', user.get('name', '')),
            'name': name, 'category': category, 'quantity': quantity, 'unit': unit,
            'price': price, 'district': district,
            'description': str(data.get('description', '')).strip(),
            'image': data.get('image', ''), 'status': 'Pending',
            'created_at': _now(),
        }
        products.insert_one(product)
        return JsonResponse({'success': True, 'product': _public_product(product)}, status=201)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid product information.'}, status=400)
    finally:
        client.close()


@csrf_protect
def api_product_detail(request, product_id):
    if request.method not in ('PATCH', 'DELETE'):
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    client = _mongo()
    try:
        db = _db(client)
        user = _current_user(db, request)
        if not user:
            return JsonResponse({'success': False, 'message': 'Please sign in.'}, status=401)
        products = _collection(db, 'produce_listings', 'products')
        product_selector = _record_selector(products, product_id)
        product = products.find_one(product_selector)
        if not product:
            return JsonResponse({'success': False, 'message': 'Product not found.'}, status=404)
        role, uid = user.get('role'), _user_id(user)
        if role != 'admin' and not (role == 'farmer' and str(product.get('farmer_id', '')) == uid):
            return JsonResponse({'success': False, 'message': 'Access denied.'}, status=403)
        if request.method == 'DELETE':
            products.delete_one(product_selector)
            return JsonResponse({'success': True})
        data = json.loads(request.body or '{}')
        allowed = ('name', 'category', 'quantity', 'unit', 'price', 'district', 'description', 'image')
        changes = {key: data[key] for key in allowed if key in data}
        if role == 'admin' and 'status' in data:
            if data['status'] not in ('Approved', 'Rejected', 'Pending'):
                return JsonResponse({'success': False, 'message': 'Invalid product status.'}, status=400)
            changes['status'] = data['status']
        elif role == 'farmer':
            changes['status'] = 'Pending'
        products.update_one(product_selector, {'$set': changes})
        return JsonResponse({'success': True, 'product': _public_product(products.find_one(product_selector))})
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid product information.'}, status=400)
    finally:
        client.close()


@csrf_protect
def api_orders(request):
    if request.method not in ('GET', 'POST'):
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    client = _mongo()
    try:
        db = _db(client)
        user = _current_user(db, request)
        if not user:
            return JsonResponse({'success': False, 'message': 'Please sign in.'}, status=401)
        orders = db['orders']
        if request.method == 'GET':
            role, uid = user.get('role'), _user_id(user)
            query = {} if role == 'admin' else ({'farmer_id': uid} if role == 'farmer' else {'buyer_id': uid})
            return JsonResponse({'success': True, 'orders': [_public_order(o) for o in orders.find(query).sort('_id', -1)]})
        if user.get('role') != 'buyer':
            return JsonResponse({'success': False, 'message': 'Only buyers can place orders.'}, status=403)
        data = json.loads(request.body or '{}')
        quantity = float(data.get('quantity', 0))
        if quantity <= 0:
            return JsonResponse({'success': False, 'message': 'Enter a valid quantity.'}, status=400)
        products = _collection(db, 'produce_listings', 'products')
        product_selector = _record_selector(products, str(data.get('productId', '')))
        product_query = {**product_selector, 'quantity': {'$gte': quantity},
                         '$or': [{'status': 'Approved'}, {'status': {'$exists': False}}]}
        product = products.find_one_and_update(
            product_query, {'$inc': {'quantity': -quantity}},
            return_document=ReturnDocument.AFTER,
        )
        if not product:
            return JsonResponse({'success': False, 'message': 'Product is unavailable or stock is insufficient.'}, status=409)
        order = {
            'id': 'ORD-' + uuid4().hex[:12].upper(), 'buyer_id': _user_id(user),
            'buyer_name': user.get('full_name', ''), 'product_id': product['id'],
            'product_name': product.get('name', ''), 'farmer_id': product.get('farmer_id', ''),
            'farmer_name': product.get('farmer_name', ''), 'quantity': quantity,
            'unit': product.get('unit', ''), 'amount': quantity * float(product.get('price', 0)),
            'status': 'Pending', 'created_at': _now(),
        }
        orders.insert_one(order)
        return JsonResponse({'success': True, 'order': _public_order(order)}, status=201)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid order information.'}, status=400)
    finally:
        client.close()


@csrf_protect
def api_order_detail(request, order_id):
    if request.method != 'PATCH':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    client = _mongo()
    try:
        db = _db(client)
        user = _current_user(db, request)
        if not user:
            return JsonResponse({'success': False, 'message': 'Please sign in.'}, status=401)
        orders = db['orders']
        order_selector = _record_selector(orders, order_id)
        order = orders.find_one(order_selector)
        if not order:
            return JsonResponse({'success': False, 'message': 'Order not found.'}, status=404)
        role, uid = user.get('role'), _user_id(user)
        if role != 'admin' and not (role == 'farmer' and str(order.get('farmer_id')) == uid):
            return JsonResponse({'success': False, 'message': 'Access denied.'}, status=403)
        status = json.loads(request.body or '{}').get('status')
        if status not in ('Pending', 'Processing', 'Completed', 'Cancelled'):
            return JsonResponse({'success': False, 'message': 'Invalid order status.'}, status=400)
        orders.update_one(order_selector, {'$set': {'status': status, 'updated_at': _now()}})
        return JsonResponse({'success': True, 'order': _public_order(orders.find_one(order_selector))})
    finally:
        client.close()


@csrf_protect
def api_users(request):
    if request.method != 'GET':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    client = _mongo()
    try:
        db = _db(client)
        user = _current_user(db, request)
        if not user or user.get('role') != 'admin':
            return JsonResponse({'success': False, 'message': 'Access denied.'}, status=403)
        found = {}
        for coll in ('users', 'farmers', 'buyers'):
            for item in db[coll].find({}):
                _migrate_legacy_profile_picture(db, item)
                pub = _public_user(item)
                if not pub['role']:
                    pub['role'] = 'farmer' if coll == 'farmers' else ('buyer' if coll == 'buyers' else '')
                if pub['id']:
                    found[pub['id']] = pub
        return JsonResponse({'success': True, 'users': list(found.values())})
    finally:
        client.close()


@csrf_protect
def api_user_detail(request, user_id):
    if request.method not in ('PATCH', 'DELETE'):
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    client = _mongo()
    try:
        db = _db(client)
        current = _current_user(db, request)
        if not current:
            return JsonResponse({'success': False, 'message': 'Please sign in.'}, status=401)
        is_admin = current.get('role') == 'admin'
        # The administrator account is stored in Django's existing auth_user collection.
        if user_id.startswith('django:'):
            if not is_admin or user_id != _user_id(current):
                return JsonResponse({'success': False, 'message': 'Access denied.'}, status=403)
            from django.contrib.auth import get_user_model
            from django.contrib.auth.hashers import make_password as _hash_password
            admin_obj = get_user_model().objects.filter(pk=user_id.split(':', 1)[1]).first()
            if not admin_obj:
                return JsonResponse({'success': False, 'message': 'Administrator account not found.'}, status=404)
            if request.method == 'DELETE':
                return JsonResponse({'success': False, 'message': 'The active administrator account cannot delete itself.'}, status=403)
            data = json.loads(request.body or '{}')
            if 'fullName' in data:
                parts = str(data['fullName']).strip().split(' ', 1)
                admin_obj.first_name = parts[0]
                admin_obj.last_name = parts[1] if len(parts) > 1 else ''
            if 'email' in data:
                admin_obj.email = str(data['email']).strip().lower()
            if 'password' in data and data['password']:
                if len(data['password']) < 6:
                    return JsonResponse({'success': False, 'message': 'Password must contain at least 6 characters.'}, status=400)
                admin_obj.set_password(data['password'])
            admin_obj.save()
            public = {'id': user_id, 'username': admin_obj.get_username(),
                      'fullName': admin_obj.get_full_name() or admin_obj.get_username(),
                      'email': admin_obj.email, 'role': 'admin', 'phone': '',
                      'district': '', 'profilePic': '', 'status': 'active',
                      'securityQuestion': '', 'createdAt': admin_obj.date_joined.isoformat() if admin_obj.date_joined else ''}
            return JsonResponse({'success': True, 'user': public})

        target = _find_user(db, user_id=user_id)
        if not target:
            return JsonResponse({'success': False, 'message': 'Account not found.'}, status=404)
        if not is_admin and _user_id(current) != user_id:
            return JsonResponse({'success': False, 'message': 'Access denied.'}, status=403)
        coll_name = 'farmers' if target.get('role') == 'farmer' else 'buyers'
        if 'users' in db.list_collection_names() and db['users'].find_one({'id': user_id}):
            coll_name = 'users'
        coll = db[coll_name]
        selector = {'id': user_id}
        if not coll.find_one(selector):
            try:
                from bson import ObjectId
                selector = {'_id': ObjectId(user_id)}
            except Exception:
                pass
        if request.method == 'DELETE':
            if not is_admin:
                return JsonResponse({'success': False, 'message': 'Access denied.'}, status=403)
            coll.delete_one(selector)
            return JsonResponse({'success': True})
        data = json.loads(request.body or '{}')
        fields = {
            'fullName': 'full_name', 'username': 'username', 'email': 'email',
            'phone': 'phone', 'district': 'district',
            'securityQuestion': 'security_question',
        }
        changes = {dst: data[src] for src, dst in fields.items() if src in data}
        if 'profilePic' in data:
            try:
                changes['profile_pic'] = _save_profile_picture(data['profilePic'])
            except ValidationError as error:
                return JsonResponse({'success': False, 'message': error.messages[0]}, status=400)
        if 'username' in changes:
            changes['username'] = str(changes['username']).strip().lower()
        if 'email' in changes:
            changes['email'] = str(changes['email']).strip().lower()
        if 'password' in data and data['password']:
            if len(data['password']) < 6:
                return JsonResponse({'success': False, 'message': 'Password must contain at least 6 characters.'}, status=400)
            changes['password_hash'] = make_password(data['password'])
        if 'securityAnswer' in data and data['securityAnswer']:
            changes['security_answer_hash'] = make_password(str(data['securityAnswer']).strip().lower())
        if is_admin and 'status' in data:
            if data['status'] not in ('active', 'suspended', 'pending'):
                return JsonResponse({'success': False, 'message': 'Invalid account status.'}, status=400)
            changes['status'] = data['status']
        if changes:
            result = coll.update_one(selector, {'$set': changes})
            old_profile = target.get('profile_pic') or target.get('profilePic', '')
            new_profile = changes.get('profile_pic', old_profile)
            if result.matched_count and old_profile != new_profile and old_profile:
                if isinstance(old_profile, str) and not old_profile.startswith('data:'):
                    old_path = old_profile[len(settings.MEDIA_URL):] if old_profile.startswith(settings.MEDIA_URL) else old_profile
                    if old_path.startswith('profile_pictures/') and not old_path.startswith('../'):
                        try:
                            default_storage.delete(old_path)
                        except Exception:
                            pass
        updated = _find_user(db, user_id=user_id)
        _migrate_legacy_profile_picture(db, updated)
        return JsonResponse({'success': True, 'user': _public_user(updated)})
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid account information.'}, status=400)
    finally:
        client.close()


@csrf_protect
def register_user(request):
    if request.method == 'GET':
        return render(request, 'register.html')
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed.'}, status=405)
    try:
        data = json.loads(request.body or '{}')
        full_name = str(data.get('fullName', '')).strip()
        username = str(data.get('username', '')).strip().lower()
        email = str(data.get('email', '')).strip().lower()
        password = data.get('password', '')
        confirm_password = data.get('confirmPassword', '')
        role = data.get('role', '')
        phone = str(data.get('phone', '')).strip()
        district = str(data.get('district', '')).strip()
        security_question = str(data.get('securityQuestion', '')).strip()
        security_answer = str(data.get('securityAnswer', '')).strip().lower()
        if not all((full_name, username, email, password, confirm_password, role, district, security_question, security_answer)):
            return JsonResponse({'success': False, 'message': 'Please fill in all required fields.'}, status=400)
        if role not in ('farmer', 'buyer'):
            return JsonResponse({'success': False, 'message': 'Invalid account type.'}, status=400)
        if len(username) < 4 or len(password) < 6:
            return JsonResponse({'success': False, 'message': 'Username must be at least 4 characters and password at least 6 characters.'}, status=400)
        if password != confirm_password:
            return JsonResponse({'success': False, 'message': 'Passwords do not match.'}, status=400)

        client = _mongo()
        try:
            db = _db(client)
            if _find_user(db, username=username):
                return JsonResponse({'success': False, 'message': 'Username already exists.'}, status=409)
            # Email uniqueness is checked across the existing user and role collections.
            for coll_name in ('users', 'farmers', 'buyers'):
                if db[coll_name].find_one({'email': email}):
                    return JsonResponse({'success': False, 'message': 'Email already registered.'}, status=409)
            user = {
                'id': str(uuid4()), 'full_name': full_name, 'username': username,
                'email': email, 'password_hash': make_password(password),
                'role': role, 'phone': phone, 'district': district,
                'security_question': security_question,
                'security_answer_hash': make_password(security_answer),
                'profile_pic': _save_profile_picture(data.get('profilePic', '')),
                'status': 'active', 'created_at': _now(),
            }
            # Save account credentials/profile in the existing role-specific collection.
            _role_collection(db, role).insert_one(user)
            return JsonResponse({'success': True, 'message': 'Account created successfully!'})
        finally:
            client.close()
    except DuplicateKeyError:
        return JsonResponse({'success': False, 'message': 'Username or email already exists.'}, status=409)
    except ValidationError as error:
        return JsonResponse({'success': False, 'message': error.messages[0]}, status=400)
    except (ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid registration information.'}, status=400)
    except Exception:
        return JsonResponse({'success': False, 'message': 'Registration could not be completed. Check the database connection and try again.'}, status=503)
