## djangoマスターへの道

### 1. リポジトリをクローンする
<br>プロジェクトを配置したいディレクトリにcdしてから以下を実施
```console
git clone https://github.com/Senshu-NEst/NEst-backend.git
```

### 2. djangoのモジュールをダウンロード<br>
```console
pip install django
pip install djangorestframework
pip install djangorestframework_simplejwt
```

### 3. 初期設定を行う
manage.pyのあるルートディレクトリで以下を実施
```console
python manage.py makemigrations
python manage.py migrate
python manage.py createsuperuser
- 好きなidとpassを入力
python manage.py runserver
```
# ENJOY YOUR DJANGO!!!
