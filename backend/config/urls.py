from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView
from api_v1.views import CustomTokenObtainPairView
from django.conf import settings
from django.conf.urls.static import static


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("api_v1.urls")),
    path("api/token/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),  # JWTログインエンドポイント
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),  # リフレッシュトークン用
]

# debug toolbar設定
if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [
        path('__debug__/', include(debug_toolbar.urls)),
    ] + urlpatterns