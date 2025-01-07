from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.db import transaction
from django.utils import timezone
from django.db.models.query import QuerySet
from datetime import datetime, timedelta
from typing import List, Dict, Tuple
from .models import Product, Stock, Transaction, Store, StockReceiveHistory, CustomUser, StockReceiveHistoryItem, ProductVariation, ProductVariationDetail
from .serializers import ProductSerializer, StockSerializer, TransactionSerializer, CustomTokenObtainPairSerializer, StockReceiveSerializer, ProductVariationSerializer, ProductVariationDetailSerializer
from rest_framework_simplejwt.views import TokenObtainPairView


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
            staff_code=CustomUser.objects.get(staff_code=staff_code),
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
        response_data = self._format_history_response(histories, jan)
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
        histories: List[StockReceiveHistory], 
        jan: str = None
    ) -> List[Dict]:
        """入荷履歴のレスポンスデータを整形"""
        response_data = []
        for history in histories:
            items = history.items.filter(jan__jan=jan) if jan else history.items.all()
            for item in items:
                response_data.append({
                    "received_at": history.received_at,
                    "staff_code": history.staff_code.staff_code,
                    #"staff_name": history.staff_code.name,
                    "store_code": history.store_code.store_code,
                    #"store_name": history.store_code.name,
                    "jan": item.jan.jan,
                    #"product_name": item.jan.name,
                    "additional_stock": item.additional_stock,
                })
        return response_data


class TransactionViewSet(viewsets.ModelViewSet):
    """取引情報に関するCRUD操作を提供するViewSet"""
    queryset = Transaction.objects.all()
    serializer_class = TransactionSerializer

    def list(self, request, *args, **kwargs) -> Response:
        """
        取引情報の一覧を取得
        
        Parameters:
            id (int, optional): 取引ID
            store_code (str, optional): 店舗コード
            staff_code (str, optional): スタッフコード
            start_date (str, optional): 開始日（YYYYMMDD形式）
            end_date (str, optional): 終了日（YYYYMMDD形式）
        """
        queryset = self._get_filtered_transactions(request)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

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

    def _get_filtered_transactions(self, request) -> QuerySet[Transaction]:
        """指定された条件で取引を検索"""
        transaction_id = request.query_params.get("id")
        store_code = request.query_params.get("store_code")
        staff_code = request.query_params.get("staff_code")
        status = request.query_params.get("status")
        start_date, end_date = self._parse_date_range(request)

        filters = {"date__range": [start_date, end_date]}
        
        if transaction_id:
            filters["id"] = transaction_id
        if store_code:
            filters["store_code__store_code"] = store_code
        if staff_code:
            filters["staff_code__staff_code"] = staff_code
        if status is None:
            filters["status"] = "sale"

        return self.queryset.filter(**filters)


class TestViewSet(viewsets.ViewSet):
    """接続テスト用のViewSet"""
    def list(self, request) -> Response:
        """サーバーの接続状態を確認するためのエンドポイント"""
        return Response({
            "message": "Connection successful!",
            "remote_address": request.META.get("REMOTE_ADDR"),
            "current_time": timezone.now(),
        })


class CustomTokenObtainPairView(TokenObtainPairView):
    """カスタマイズされたJWTトークン発行View"""
    serializer_class = CustomTokenObtainPairSerializer
