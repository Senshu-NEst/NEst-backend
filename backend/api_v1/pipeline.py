from .utils import generate_random_password
from django.contrib.auth import get_user_model, login
from social_core.pipeline.partial import partial

User = get_user_model()

@partial
def create_or_update_user(backend, user=None, response=None, *args, **kwargs):
    if backend.name == 'google-oauth2':
        email = response.get('email')

        # 既存のユーザーを確認
        existing_user = User.objects.filter(email=email).first()

        if existing_user:
            # 既存ユーザーのsocial authが存在しない場合は作成
            if not existing_user.social_auth.filter(provider='google-oauth2').exists():
                existing_user.social_auth.create(
                    provider='google-oauth2',
                    uid=response.get('sub'),
                    extra_data=response
                )
            return existing_user

        # 新しいユーザーを作成
        new_user = User.objects.create(
            email=email,
            user_type='customer',
            is_active=True
        )

        # ランダムなパスワードを生成
        random_password = generate_random_password(length=12)
        new_user.set_password(random_password)
        new_user.save()

        # social_authのアカウントを紐づけ
        new_user.social_auth.create(
            provider='google-oauth2',
            uid=response.get('sub'),
            extra_data=response
        )

        return new_user
