from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView
from api_v1.views import CustomTokenObtainPairView, login_view, google_login_redirect, profile_view, logout_view
from django.conf import settings


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("api_v1.urls")),
    path("api/token/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),  # JWTログインエンドポイント
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),  # リフレッシュトークン用
    path('google-login/', google_login_redirect, name='google_login'),  # Googleログイン
    path('auth/', include('social_django.urls', namespace='social')),  # social-authのURL
    path('login/', login_view, name='login'),  # ログインページのURL
    path('logout/', logout_view, name='logout'),  # ログアウト用のURL
    path('profile/', profile_view, name='profile'),  # プロフィール表示用のURL
]

# debug toolbar設定
if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [
        path('__debug__/', include(debug_toolbar.urls)),
    ] + urlpatterns
