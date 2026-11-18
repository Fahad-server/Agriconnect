"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from core import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', views.home, name='home'),
    path('login/', views.login_page, name='login'),
    path('register/', views.register_user, name='register'),
    path('api/login/', views.api_login, name='api_login'),
    path('api/logout/', views.api_logout, name='api_logout'),
        path('api/password-reset/', views.api_password_reset, name='api_password_reset'),
    path('api/products/', views.api_products, name='api_products'),
    path('api/products/<str:product_id>/', views.api_product_detail, name='api_product_detail'),
    path('api/orders/', views.api_orders, name='api_orders'),
    path('api/orders/<str:order_id>/', views.api_order_detail, name='api_order_detail'),
        path('api/users/', views.api_users, name='api_users'),
        path('api/users/<str:user_id>/', views.api_user_detail, name='api_user_detail'),
    path('products/', views.products_page, name='products'),
    path('market-prices/', views.market_prices_page, name='market_prices'),
    path('about/', views.about_page, name='about'),
    path('admin-dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('farmer-dashboard/', views.farmer_dashboard, name='farmer_dashboard'),
    path('buyer-dashboard/', views.buyer_dashboard, name='buyer_dashboard'),
    path('dashboard/', views.legacy_dashboard, name='legacy_dashboard'),
]

from django.urls import re_path
from django.views.static import serve

urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]