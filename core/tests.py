import base64
from io import BytesIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from django.core.files.storage import default_storage
from django.test import SimpleTestCase
from django.test import override_settings
from PIL import Image

from core.views import _migrate_legacy_profile_picture, _profile_picture_url, _save_profile_picture


class PageNavigationTests(SimpleTestCase):
	def test_profile_picture_data_is_saved_as_a_media_file(self):
		image = BytesIO()
		Image.new('RGB', (2, 2), color='green').save(image, format='JPEG')
		data_uri = 'data:image/jpeg;base64,' + base64.b64encode(image.getvalue()).decode('ascii')

		with TemporaryDirectory() as media_root:
			with override_settings(MEDIA_ROOT=media_root):
				path = _save_profile_picture(data_uri)
				self.assertTrue(path.startswith('profile_pictures/'))
				self.assertTrue(default_storage.exists(path))
				self.assertEqual(_profile_picture_url(path), f'/media/{path}')

	def test_legacy_profile_picture_is_replaced_with_a_file_path(self):
		image = BytesIO()
		Image.new('RGB', (2, 2), color='green').save(image, format='JPEG')
		data_uri = 'data:image/jpeg;base64,' + base64.b64encode(image.getvalue()).decode('ascii')
		users = Mock()
		users.update_one.return_value = SimpleNamespace(matched_count=1)
		db = {'users': users, 'farmers': Mock(), 'buyers': Mock()}
		user = {'_id': 'user-id', 'profile_pic': data_uri}

		with TemporaryDirectory() as media_root:
			with override_settings(MEDIA_ROOT=media_root):
				path = _migrate_legacy_profile_picture(db, user)

		self.assertTrue(path.startswith('profile_pictures/'))
		self.assertNotIn('profilePic', user)
		self.assertEqual(user['profile_pic'], path)
		update = users.update_one.call_args.args[1]
		self.assertEqual(update['$set']['profile_pic'], path)
		self.assertIn('profilePic', update['$unset'])

	def test_existing_pages_render(self):
		page_paths = (
			'/',
			'/login/',
			'/register/',
			'/products/',
			'/market-prices/',
			'/about/',
			'/admin-dashboard/',
			'/farmer-dashboard/',
			'/buyer-dashboard/',
			'/dashboard/',
		)

		for page_path in page_paths:
			with self.subTest(page_path=page_path):
				response = self.client.get(page_path)
				self.assertEqual(response.status_code, 200)

	def test_api_login_rejects_non_post_requests(self):
		response = self.client.get('/api/login/')
		self.assertEqual(response.status_code, 405)

	def test_marketplace_apis_require_a_session(self):
		for endpoint in ('/api/products/', '/api/orders/', '/api/users/'):
			with self.subTest(endpoint=endpoint):
				response = self.client.get(endpoint)
				self.assertEqual(response.status_code, 401)
