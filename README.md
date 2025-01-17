## バックエンドマスターへの道

### 1. リポジトリをクローンする<br>
プロジェクトを配置したいディレクトリにcdしてから以下を実施
```console
git clone https://github.com/Senshu-NEst/NEst-backend.git
```

### 2. docker-composeでビルドを実行<br>
⚠️docker-composeのバージョンによってハイフン(`docker-compose`, `docker compose`)の有無が異なるので注意⚠️<br>
```console
docker-compose up -d --build
```
何度もコンテナを構築しているとコンテナに紐づくゴミファイルが溜まってビルドできない時がある。その場合は以下を実行。
```console
docker system prune
```
※不要なコンテナやimagesが一括削除される。<br>

### 3. 初期設定を行う<br>
djangoを実行しているコンテナに入り、スーパーユーザーを作成する
```console
docker-compose exec api bash
python manage.py createsuperuser
- 任意のidとpassを入力
exit
```
# ENJOY YOUR BACKEND!!