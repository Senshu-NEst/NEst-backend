# pythonライブラリ
import random
import requests
import qrcode
import io
import base64
from datetime import datetime, timedelta, time, date
from typing import List, Dict, Tuple
# Djangoのライブラリ
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db import transaction
from django.db.models.query import QuerySet
import rules
# DRFライブラリ
from rest_framework import viewsets, status, mixins
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import action, api_view
from rest_framework.authtoken.models import Token
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.settings import api_settings
# モデル
from .models import Product, Stock, Transaction, Store, StockReceiveHistory, CustomUser, StockReceiveHistoryItem, ProductVariation, Staff, Approval, ReturnTransaction, Customer
# シリアライザー
from .serializers import ProductSerializer, StockSerializer, TransactionSerializer, CustomTokenObtainPairSerializer, StockReceiveSerializer, ProductVariationSerializer, WalletChargeSerializer, WalletBalanceSerializer, CustomUserTokenSerializer, ApprovalSerializer, ReturnTransactionSerializer, StaffSerializer, CustomerSerializer
# 自作モジュール
from .get_receipt_data import generate_receipt_text, generate_return_receipt
from .rules import check_transaction_access, filter_transactions_by_user


class ProductViewSet(viewsets.ModelViewSet):
    """商品情報に関するCRUD操作を提供するViewSet"""
    queryset = Product.objects.all()
    serializer_class = ProductSerializer


class ProductVariationViewSet(viewsets.ModelViewSet):
    """商品バリエーション情報を提供するViewSet"""
    queryset = ProductVariation.objects.all()
    serializer_class = ProductVariationSerializer


class StockViewSet(viewsets.ModelViewSet):
    """在庫情報に関するCRUD操作と入荷処理を提供するViewSet"""
    queryset = Stock.objects.all()
    serializer_class = StockSerializer

    def list(self, request, *args, **kwargs) -> Response:
        """
        店舗コードまたはJANコードに基づいて在庫情報を取得
        
        Parameters:
            store_code (str, optional): 店舗コード
            jan (str, optional): JANコード
        """
        store_code = request.query_params.get("store_code")
        jan = request.query_params.get("jan")

        if not store_code and not jan:
            return Response(
                {"error": "店舗コードまたはJANコードが必要です。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        stocks = self._get_filtered_stocks(store_code, jan)
        serializer = StockSerializer(stocks, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _get_filtered_stocks(self, store_code: str = None, jan: str = None) -> List[Stock]:
        """指定された条件に基づいて在庫を検索"""
        filters = {}
        if store_code:
            filters["store_code__store_code"] = store_code
        if jan:
            filters["jan__jan"] = jan
        return Stock.objects.filter(**filters)

    @action(detail=False, methods=["post"])
    def receive(self, request) -> Response:
        """入荷処理のエンドポイント"""
        serializer = StockReceiveSerializer(data=request.data)
        if serializer.is_valid():
            return self._process_stock_receive(serializer)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def _process_stock_receive(self, serializer: StockReceiveSerializer) -> Response:
        """入荷処理のメイン処理"""
        store_code = serializer.validated_data["store_code"]
        staff_code = serializer.validated_data["staff_code"]
        items = serializer.validated_data["items"]

        try:
            with transaction.atomic():
                history = self._create_receive_history(store_code, staff_code)
                received_items = self._process_received_items(items, history)

                return Response({
                    "store_code": store_code,
                    "staff_code": staff_code,
                    "received_at": history.received_at.isoformat(),
                    "items": received_items,
                }, status=status.HTTP_200_OK)

        except Exception as e:
            return Response(
                {"error": f"入荷処理中にエラーが発生しました: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

    def _create_receive_history(self, store_code: str, staff_code: str) -> StockReceiveHistory:
        """入荷履歴レコードを作成"""
        return StockReceiveHistory.objects.create(
            store_code=Store.objects.get(store_code=store_code),
            staff_code=Staff.objects.get(staff_code=staff_code),
        )

    def _process_received_items(
        self,
        items: List[Dict],
        history: StockReceiveHistory
    ) -> List[Dict]:
        """各商品の入荷処理と履歴項目の作成"""
        processed_items = []
        for item in items:
            product = Product.objects.get(jan=item["jan"])
            stock = self._update_or_create_stock(history.store_code, product, item["additional_stock"])
            self._create_history_item(history, product, item["additional_stock"])
            processed_items.append({
                "jan": product.jan,
                "additional_stock": item["additional_stock"]
            })
        return processed_items

    def _update_or_create_stock(
        self, 
        store: Store, 
        product: Product, 
        additional_stock: int
    ) -> Stock:
        """在庫の更新または新規作成"""
        stock, _ = Stock.objects.get_or_create(
            store_code=store,
            jan=product,
            defaults={"stock": 0}
        )
        stock.stock += additional_stock
        stock.save()
        return stock

    def _create_history_item(
        self, 
        history: StockReceiveHistory, 
        product: Product, 
        additional_stock: int
    ) -> StockReceiveHistoryItem:
        """入荷商品を中間テーブルに挿入"""
        return StockReceiveHistoryItem.objects.create(
            history=history,
            jan=product,
            additional_stock=additional_stock,
        )


class StockReceiveHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """入荷履歴の参照機能を提供するViewSet"""
    queryset = StockReceiveHistory.objects.all()

    def list(self, request, *args, **kwargs) -> Response:
        """
        入荷履歴の一覧を取得
        
        Parameters:
            store_code (str, optional): 店舗コード
            staff_code (str, optional): スタッフコード
            jan (str, optional): JANコード
            start_date (str, optional): 開始日（YYYYMMDD形式）
            end_date (str, optional): 終了日（YYYYMMDD形式）
        """
        store_code = request.query_params.get("store_code")
        staff_code = request.query_params.get("staff_code")
        jan = request.query_params.get("jan")
        start_date, end_date = self._parse_date_range(request)

        histories = self._get_filtered_histories(
            store_code=store_code,
            staff_code=staff_code,
            start_date=start_date,
            end_date=end_date
        )
        response_data = self._format_history_response(histories)
        return Response(response_data)

    def _parse_date_range(self, request) -> Tuple[datetime, datetime]:
        """日付範囲パラメータの解析"""
        # 開始日・終了日の指定がない場合は当日を指定
        default_date = datetime.now().strftime("%Y%m%d")
        
        start_date = request.query_params.get("start_date", default_date)
        end_date = request.query_params.get("end_date", default_date)

        start = datetime.strptime(start_date, "%Y%m%d")
        # 終了日の場合は日付の最後（23:59:59）までを含める
        end = datetime.strptime(end_date, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)

        return start, end

    def _get_filtered_histories(
        self,
        store_code: str = None,
        staff_code: str = None,
        start_date: datetime = None,
        end_date: datetime = None
    ) -> List[StockReceiveHistory]:
        """指定された条件で入荷履歴を検索"""
        filters = {"received_at__range": [start_date, end_date]}
        
        if store_code:
            filters["store_code__store_code"] = store_code
        if staff_code:
            filters["staff_code__staff_code"] = staff_code
            
        return StockReceiveHistory.objects.filter(**filters).distinct()

    def _format_history_response(
        self,
        histories: List[StockReceiveHistory]
    ) -> List[Dict]:
        """入荷履歴のレスポンスデータを整形"""
        response_data = []
        for history in histories:
            items = history.items.all()  # この履歴に関連する全アイテムを取得
            
            # アイテムをリストとしてまとめる
            item_list = []
            for item in items:
                item_list.append({
                    "jan": item.jan.jan,
                    "additional_stock": item.additional_stock,
                    # "product_name": item.jan.name,  # 商品名を追加する場合
                })
            
            # 履歴ごとにまとめてレスポンスに追加
            response_data.append({
                "received_at": history.received_at,
                "staff_code": history.staff_code.staff_code,
                # "staff_name": history.staff_code.name,  # スタッフ名を追加する場合
                "store_code": history.store_code.store_code,
                # "store_name": history.store_code.name,  # 店舗名を追加する場合
                "items": item_list  # アイテムリストを追加
            })
        return response_data

    def get_queryset(self):
        user = self.request.user
        return filter_transactions_by_user(user, Transaction.objects.all())


class TransactionViewSet(viewsets.ModelViewSet):
    """【指定可能なクエリパラメータ】
        - id (int, optional): 取引ID。指定された場合、特定の取引を検索します。
        - store_code (str, optional): 店舗コード。取引の店舗コードで絞り込みます。
        - staff_code (str, optional): スタッフコード。取引の担当スタッフで絞り込みます。
        - terminal_id (str, optional): 端末ID。取引が行われた端末で絞り込みます。
        - start_date (str, optional): 開始日（YYYYMMDD形式）。デフォルトは当日の日付。
        - end_date (str, optional): 終了日（YYYYMMDD形式）。デフォルトは当日の日付。
        - status (str, optional): 取引状態。 'all' 以外の場合、状態による絞り込みを行います。デフォルトはsale。
    【注意点】
        - ユーザーの所属店舗・権限に基づき、アクセス可能な取引のみが表示されます。
        - パラメータの指定がない場合、デフォルトで当日の日付範囲かつ status="sale" の取引のみが表示されます。
        - クエリパラメータは URL のクエリ文字列として渡してください。
    """
    queryset = Transaction.objects.all()
    serializer_class = TransactionSerializer

    def get_queryset(self):
        user = self.request.user
        return filter_transactions_by_user(user, Transaction.objects.all())

    def list(self, request, *args, **kwargs) -> Response:
        queryset = self.get_queryset()
        filtered_queryset = self._get_filtered_transactions(request, queryset)
        serializer = self.get_serializer(filtered_queryset, many=True)
        return Response(serializer.data)

    def _parse_date_range(self, request) -> tuple:
        """日付範囲を解析する
        【仕様】
        - 両方指定されている場合：start_date～end_dateの範囲
        - start_date のみ指定された場合：start_date以降（終了日は 9999/12/31 23:59:59 とする）
        - end_date のみ指定された場合：end_date以前（開始日は 1900/01/01 00:00:00 とする）
        - いずれも指定されない場合：当日の範囲（00:00:00 ～ 23:59:59）
        """
        start_date_str = self.request.query_params.get("start_date", None)
        end_date_str = self.request.query_params.get("end_date", None)
        
        if start_date_str and end_date_str:
            start = datetime.strptime(start_date_str, "%Y%m%d")
            end = datetime.strptime(end_date_str, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
        elif start_date_str:
            start = datetime.strptime(start_date_str, "%Y%m%d")
            end = datetime(9999, 12, 31, 23, 59, 59)
        elif end_date_str:
            start = datetime(1900, 1, 1, 0, 0, 0)
            end = datetime.strptime(end_date_str, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
        else:
            today = date.today()
            start = datetime.combine(today, time.min)
            end = datetime.combine(today, time.max)
        return start, end

    def _get_filtered_transactions(self, request, queryset) -> 'QuerySet[Transaction]':
        transaction_id = request.query_params.get("id")
        store_code = request.query_params.get("store_code")
        staff_code = request.query_params.get("staff_code")
        terminal_id = request.query_params.get("terminal_id")
        status_param = request.query_params.get("status")
        start_date, end_date = self._parse_date_range(request)

        filters = {}
        if transaction_id:
            filters["id"] = transaction_id
        else:
            filters["date__range"] = [start_date, end_date]
        if store_code:
            filters["store_code"] = store_code
        if staff_code:
            filters["staff_code"] = staff_code
        if terminal_id:
            filters["terminal_id"] = terminal_id

        # status が未指定の場合は常に "sale" を適用
        if not status_param and not transaction_id:
            filters["status"] = "sale"
        elif transaction_id:
            pass
        elif status_param != 'all':
            filters["status"] = status_param
        else:
            pass

        filtered_queryset = queryset.filter(**filters)
        if transaction_id:
            transaction = Transaction.objects.filter(id=transaction_id).first()
            if not transaction:
                raise NotFound("指定された取引は存在しません。")
            #check_transaction_access(request.user, transaction)
        return filtered_queryset


class ReturnTransactionViewSet(viewsets.ModelViewSet):
    """【指定可能なクエリパラメータ】
        - id (int, optional): 返品取引ID。特定の返品取引を検索します。
        - origin_id (int, optional): 元取引のID。元取引IDにより、該当する返品取引を絞り込みます。
        - store_code (str, optional): 店舗コード。元取引の店舗コードで絞り込みます。
        - staff_code (str, optional): 返品担当スタッフコード。返品取引の担当スタッフで絞り込みます。
        - terminal_id (str, optional): 端末ID。返品取引の端末IDで絞り込みます。
        - start_date (str, optional): 開始日（YYYYMMDD形式）。デフォルトは当日の日付。
        - end_date (str, optional): 終了日（YYYYMMDD形式）。デフォルトは当日の日付。
    【注意点】
        - ユーザーの所属店舗・権限に基づき、アクセス可能な取引のみが表示されます。
        - 元取引の閲覧権限がない場合、返品取引も閲覧できません。
    """
    queryset = ReturnTransaction.objects.all()
    serializer_class = ReturnTransactionSerializer
    #permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return filter_transactions_by_user(user, ReturnTransaction.objects.all())

    def list(self, request, *args, **kwargs) -> Response:
        queryset = self.get_queryset()
        filtered_queryset = self._get_filtered_return_transactions(request, queryset)
        serializer = self.get_serializer(filtered_queryset, many=True)
        return Response(serializer.data)

    def _parse_date_range(self, request) -> tuple:
        start_date_str = request.query_params.get("start_date", None)
        end_date_str = request.query_params.get("end_date", None)
        
        if start_date_str and end_date_str:
            start = datetime.strptime(start_date_str, "%Y%m%d")
            end = datetime.strptime(end_date_str, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
        elif start_date_str:
            start = datetime.strptime(start_date_str, "%Y%m%d")
            end = datetime(9999, 12, 31, 23, 59, 59)
        elif end_date_str:
            start = datetime(1900, 1, 1, 0, 0, 0)
            end = datetime.strptime(end_date_str, "%Y%m%d") + timedelta(days=1) - timedelta(seconds=1)
        else:
            today = date.today()
            start = datetime.combine(today, time.min)
            end = datetime.combine(today, time.max)
        return start, end

    def _get_filtered_return_transactions(self, request, queryset) -> 'QuerySet[ReturnTransaction]':
        return_id = request.query_params.get("id")
        origin_id = request.query_params.get("origin_id")
        store_code = request.query_params.get("store_code")
        staff_code = request.query_params.get("staff_code")
        terminal_id = request.query_params.get("terminal_id")
        start_date, end_date = self._parse_date_range(request)

        filters = {}
        if return_id:
            filters["id"] = return_id
        else:
            filters["return_date__range"] = [start_date, end_date]
        if origin_id:
            filters["origin_transaction__id"] = origin_id
        if store_code:
            filters["origin_transaction__store_code"] = store_code
        if staff_code:
            filters["staff_code__staff_code"] = staff_code
        if terminal_id:
            filters["terminal_id"] = terminal_id

        filtered_queryset = queryset.filter(**filters)
        if return_id:
            return_transaction = queryset.filter(id=return_id).first()
            if not return_transaction:
                raise NotFound("指定された返品取引は存在しません。")
            check_transaction_access(request.user, return_transaction.origin_transaction)
        return filtered_queryset

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return_transaction = serializer.save()
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class TestViewSet(viewsets.ViewSet):
    """接続テスト用のViewSet"""
    def list(self, request) -> Response:
        """サーバーの接続状態を確認するためのエンドポイント"""
        return Response({
            "message": "Connection successful!",
            "remote_address": request.META.get("REMOTE_ADDR"),
            "current_time": timezone.now(),
        })


class WalletViewSet(viewsets.ViewSet):

    @action(detail=False, methods=['post'], url_path='charge')
    def charge(self, request):
        serializer = WalletChargeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user_id = serializer.validated_data['user_id']
        amount = serializer.validated_data['amount']

        try:
            user = CustomUser.objects.get(id=user_id)

            with transaction.atomic():  # トランザクションで保護
                new_balance, created = serializer.create_wallet_transaction(user, amount)

            # メッセージを設定
            if created and amount == 0:
                message = "新規にウォレットを作成しました。"
            elif created:
                message = "新規にウォレットを作成し、チャージが成功しました。"
            else:
                message = "チャージが成功しました。"

            return Response({
                "message": message,
                "amount": str(amount),  # チャージ金額を追加
                "new_balance": str(new_balance)
            }, status=status.HTTP_200_OK)
        except CustomUser.DoesNotExist:
            return Response({"error": "指定されたユーザーは存在しません。"}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='balance')
    def get_balance(self, request):
        serializer = WalletBalanceSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)

        user_id = serializer.validated_data['user_id']

        try:
            user = CustomUser.objects.get(id=user_id)
            wallet = user.wallet
            return Response({"user_id": user_id, "balance": str(wallet.balance)}, status=status.HTTP_200_OK)
        except CustomUser.DoesNotExist:
            return Response({"error": "指定されたユーザーは存在しません。"}, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenObtainPairView(TokenObtainPairView):
    """カスタマイズされたJWTトークン発行View"""
    serializer_class = CustomTokenObtainPairSerializer

@login_required
def generate_receipt_view(request, transaction_id, receipt_type):

    # アクセス権をチェック
    # TODO:返品レシートの権限チェックが未実装のため一旦コメントアウト
    #if not rules.test_rule('view_transaction', request.user, transaction):
    #    return HttpResponseForbidden("この取引にアクセスする権限がありません。")

    # レシート種別に応じたテキスト生成
    if receipt_type == 'sale':
        transaction = get_object_or_404(Transaction, id=transaction_id)
        receipt_text = generate_receipt_text(transaction)
    elif receipt_type == 'return':
        transaction = get_object_or_404(ReturnTransaction, id=transaction_id)
        receipt_text = generate_return_receipt(transaction)
    else:
        return HttpResponse("無効な取引種別", content_type="text/plain")

    # レシートデータを外部サービスにPOST送信
    response = requests.post("http://receipt:6573/generate", data={"text": receipt_text})
    if response.status_code == 200:
        html_content = response.text
        return HttpResponse(html_content, content_type="text/html; charset=utf-8")
    else:
        return HttpResponse("レシートデータの取得に失敗しました", content_type="text/plain")


def login_view(request):
    return render(request, 'login.html')


def logout_view(request):
    logout(request)  # ユーザーをログアウト
    return render(request, 'logout.html')


def google_login_redirect(request):
    if request.user.is_authenticated:
        logout(request)

    # Googleのログインを開始
    return redirect('social:begin', 'google-oauth2')


@login_required
def profile_view(request):
    user = request.user
    # トークンを生成する
    token = SimpleAccessToken.for_user(user)
    # 有効期限を計算
    expires_timestamp = token["exp"]  # UNIXtime
    expires_datetime = timezone.datetime.fromtimestamp(expires_timestamp)
    expires_formatted = expires_datetime.strftime("%Y-%m-%d %H:%M:%S")

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(str(token))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()

    return render(request, 'api_v1/profile.html', {
        'user': user,
        'token': str(token),
        'expires': expires_formatted,
        'qr_code': img_str,
    })


class SimpleAccessToken(AccessToken):
    @classmethod
    def for_user(cls, user):
        """
        ユーザー情報からアクセストークンを生成する。
        """
        token = cls()
        # ユーザーIDをトークンにセット（文字列に変換）
        token["user_id"] = str(user.pk)
        # 有効期限は設定に従い自動的に設定する代わり、ここで明示的にセット
        lifetime = api_settings.ACCESS_TOKEN_LIFETIME
        token.set_exp(from_time=timezone.now(), lifetime=lifetime)
        return token


class CustomUserTokenViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    """
    ログイン状態（認証済み）のユーザーに対して、
    django-simplejwt を用いてアクセストークン（有効期限付き）を発行するエンドポイント。
    """
    permission_classes = [IsAuthenticated]
    serializer_class = CustomUserTokenSerializer  # POST 用シリアライザー（ただし出力は create() 内で返す）

    def create(self, request, *args, **kwargs):
        user = request.user
        # SimpleAccessToken を使ってアクセストークンを生成（staff_code などは付与しない）
        token = SimpleAccessToken.for_user(user)
        return Response({
            "token": str(token),
            "expires": token["exp"],
            "user_id": user.pk,
        }, status=status.HTTP_200_OK)

    def list(self, request, *args, **kwargs):
        # GET リクエストは受け付けず 405 を返す
        return Response({"detail": "Method 'GET' not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)


class ApprovalViewSet(mixins.CreateModelMixin, viewsets.GenericViewSet):
    permission_classes = [AllowAny]
    queryset = Approval.objects.all()
    serializer_class = ApprovalSerializer

    def create(self, request, *args, **kwargs):
        token_str = request.data.get("token")
        if not token_str:
            return Response({"error": "tokenパラメータが必要です。"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            access_token = AccessToken(token_str)
        except TokenError:
            return Response({"error": "トークンが無効または期限切れです。"}, status=status.HTTP_400_BAD_REQUEST)

        # JWT に含まれる user_id を取得
        user_id = access_token.get("user_id")
        if not user_id:
            return Response({"error": "トークンにユーザー情報が含まれていません。"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = CustomUser.objects.get(pk=user_id)
        except CustomUser.DoesNotExist:
            return Response({"error": "ユーザーが存在しません。"}, status=status.HTTP_400_BAD_REQUEST)

        # ユーザーが持つ未使用の承認番号を取得
        existing_approval = Approval.objects.filter(user=user, is_used=False).first()
        if existing_approval:
            # 未使用の承認番号がある場合は削除
            existing_approval.delete()

        # 新しい承認番号を生成する関数
        def generate_unique_approval_number():
            while True:
                approval_number = f"{random.randint(0, 99999999):08d}"
                if not Approval.objects.filter(approval_number=approval_number).exists():
                    return approval_number

        # 承認番号を生成（重複チェック）
        approval_number = generate_unique_approval_number()

        # Approval モデルに保存（CustomUser を親として外部キーで紐付け）
        Approval.objects.create(user=user, approval_number=approval_number, is_used=False)

        return Response({"approval_number": approval_number}, status=status.HTTP_201_CREATED)


class TokenVerifyViewSet(viewsets.ViewSet):
    """
    POSTされたJWTを検証し、トークンが有効な場合にユーザー情報を返すViewSet
    """
    permission_classes = []  # 認証不要

    def create(self, request, *args, **kwargs):
        token_str = request.data.get("token")
        if not token_str:
            return Response({"error": "tokenパラメータが必要です。"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # トークンのデコードと検証
            access_token = AccessToken(token_str)
            user_id = access_token.get("user_id")
            if not user_id:
                raise TokenError("トークンにユーザー情報が含まれていません。")

        except TokenError as e:
            return Response({"error": f"トークンが無効または期限切れです: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = CustomUser.objects.get(pk=user_id)
        except CustomUser.DoesNotExist:
            return Response({"error": "ユーザーが存在しません。"}, status=status.HTTP_400_BAD_REQUEST)

        data = {}
        try:
            staff = Staff.objects.get(user=user)
            data['staff'] = StaffSerializer(staff).data
        except Staff.DoesNotExist:
            pass

        try:
            customer = Customer.objects.get(user=user)
            data['customer'] = CustomerSerializer(customer).data
        except Customer.DoesNotExist:
            pass

        return Response(data, status=status.HTTP_200_OK)