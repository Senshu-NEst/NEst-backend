FROM node:18-bullseye-slim

WORKDIR /app

# パッケージファイルと実行ファイルをコピー
COPY ./containers/receiptline/package.json /app/
COPY ./containers/receiptline/receipt_server.js /app/


# 依存関係をインストール
RUN npm install

# エントリーポイントスクリプトをコピーし、実行権限を付与
COPY ./containers/receiptline/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# エントリーポイントスクリプトを実行
ENTRYPOINT ["entrypoint.sh"]
