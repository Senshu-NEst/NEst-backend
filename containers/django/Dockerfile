FROM python:3.12-slim-bullseye
# PYTHONDONTWRITEBYTECODEとPYTHONUNBUFFEREDはオプション
# pycファイル(および__pycache__)の生成を行わないようにする
ENV PYTHONDONTWRITEBYTECODE=1
# 標準出力・標準エラーのストリームのバッファリングを行わない
ENV PYTHONUNBUFFERED=1

WORKDIR /api

# ローカルのrequirements.txtをコンテナの/apiフォルダ直下に置く
COPY ./containers/django/requirements.txt /api/
# コンテナ内でpipをアップグレードした後、pip install -r requirements.txtを実行
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ソースコードはボリュームマウント（本番環境ではコピーを行う）
#COPY ./api/

# エントリーポイントをコピーし実行権限を付与
COPY ./containers/django/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# エントリーポイントスクリプトを実行
ENTRYPOINT ["entrypoint.sh"]