from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, Customer
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class UserPermissionChecker:
    def __init__(self, staff_code):
        self.staff_code = staff_code

    def get_permissions(self):
        """スタッフの権限を取得して、権限リストを返す"""
        try:
            # staff_codeを持つStaffを直接取得
            staff = Staff.objects.select_related("permission", "user").get(staff_code=self.staff_code)

            # CustomUserのis_activeを確認
            if not staff.user.is_active:
                raise serializers.ValidationError("このスタッフは現在無効です。")

            permissions = []
            if staff.user.is_superuser:
                permissions.append("superuser")

            # スタッフの権限を取得
            if not staff.permission:
                raise serializers.ValidationError("このスタッフに権限が設定されていません。")

            # 権限の追加
            permission_fields = [
                ("register_permission", "register"),
                ("global_permission", "global"),
                ("change_price_permission", "change_price"),
                ("stock_receive_permission", "stock_receive"),
            ]

            for field, name in permission_fields:
                if getattr(staff.permission, field):
                    permissions.append(name)

            return permissions

        except Staff.DoesNotExist:
            raise serializers.ValidationError("指定されたスタッフが存在しません。")


class PaymentSerializer(serializers.ModelSerializer):
    payment_method = serializers.ChoiceField(choices=Payment.PAYMENT_METHOD_CHOICES)
    amount = serializers.IntegerField()

    class Meta:
        model = Payment
        fields = ['payment_method', 'amount']


class TransactionDetailSerializer(serializers.ModelSerializer):
    tax = serializers.IntegerField(read_only=True)  # Intに変換しないとDRFではDecimalは文字列に変換されてしまう
    discount = serializers.IntegerField(required=False, default=0)

    class Meta:
        model = TransactionDetail
        fields = ["jan", "name", "price", "tax", "discount", "quantity"]
        read_only_fields = ["name", "price", "tax"]


class TransactionSerializer(serializers.ModelSerializer):
    sale_products = TransactionDetailSerializer(many=True)
    payments = PaymentSerializer(many=True)
    date = serializers.DateTimeField(required=False)

    class Meta:
        model = Transaction
        fields = ["id", "status", "date", "store_code", "terminal_id", "staff_code", "user", "total_tax10", "total_tax8", "tax_amount", "discount_amount", "total_amount", "deposit", "change", "total_quantity", "payments", "sale_products"]
        read_only_fields = ["id", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount", "deposit"]

    def validate_status(self, value):
        if value == "void":
            raise serializers.ValidationError("無効なstatusが指定されました。")
        return value

    def validate_sale_products(self, value):
        if not value:
            raise serializers.ValidationError("少なくとも1つの商品を指定してください。")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("支払方法を登録してください。")
        return value

    def create(self, validated_data):
        sale_products_data = validated_data.pop("sale_products", [])
        payments_data = validated_data.pop("payments", [])
        staff_code = validated_data.pop("staff_code", None)  # staff_codeをpopする
        store_code = validated_data.pop("store_code", None)  # store_codeをpopする
        status = validated_data.get("status")
        user = validated_data.pop("user", None)  # userをpopする
        terminal_id = validated_data.pop("terminal_id", None)  # terminal_idをpopする

        # 権限のチェック
        permissions = self._check_permissions(staff_code, store_code)
        # 値引きがある場合のチェック
        self._discount_check(sale_products_data, store_code, permissions)

        with transaction.atomic():
            # 合計金額と支払い金額の計算
            totals = self._calculate_totals(sale_products_data, store_code)
            total_payments = sum(payment['amount'] for payment in payments_data)
            
            # トランザクションの作成
            transaction_instance = Transaction.objects.create(
                date=timezone.now(),
                user=user,  # userフィールドを設定
                terminal_id=terminal_id,  # terminal_idを設定
                staff_code=staff_code,  # staff_codeを設定
                store_code=store_code,  # store_codeを設定
                deposit=total_payments,
                change=total_payments - totals['total_amount'],
                total_quantity=totals['total_quantity'],
                total_tax10=totals['total_tax10'],
                total_tax8=totals['total_tax8'],
                tax_amount=totals['tax_amount'],
                discount_amount=totals['discount_amount'],
                total_amount=totals['total_amount'],
                **validated_data  # その他のフィールドを設定
            )

            # 取引詳細の作成
            self._create_transaction_details(transaction_instance, sale_products_data, status, store_code)

            # 支払データの保存
            self._create_payments(transaction_instance, payments_data)

            # 支払方法のバリデーション
            self._validate_payments(total_payments, totals['total_amount'], payments_data)

        return transaction_instance

    def _discount_check(self, sale_products_data, store_code, permissions):
        # 値引きがある際に行う共通処理
        for sale_product in sale_products_data:
            if sale_product.get("discount", 0) != 0 and "change_price" not in permissions:
                raise serializers.ValidationError("このスタッフは売価変更を行う権限がありません。")

            product = self._get_product(sale_product["jan"])
            store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
            effective_price = store_price.get_price() if store_price else product.price

            # 割引が商品価格を超えていないかチェック、割引額は自然数のみ
            discount = sale_product.get("discount", 0)
            if discount > effective_price or discount < 0:
                raise serializers.ValidationError(f"JAN:{sale_product['jan']} 不正な割引額が入力されました。")

    def _check_permissions(self, staff_code, store_code):
        # 権限のチェック
        permission_checker = UserPermissionChecker(staff_code)
        permissions = permission_checker.get_permissions()

        # 所属店舗をStaffモデルから取得
        staff = Staff.objects.get(staff_code=staff_code)

        # レジ権限のチェック
        if "register" not in permissions:
            raise serializers.ValidationError("このスタッフは販売を行う権限がありません。")

        # staffの所属店舗とPOSTされたstore_codeが異なる場合にglobal権限をチェック
        if staff.affiliate_store.store_code != store_code:
            if "global" not in permissions:
                raise serializers.ValidationError("このスタッフは自店のみ処理可能です。")

        return permissions

    def _calculate_totals(self, sale_products_data, store_code):
        total_quantity = 0
        total_amount_tax10 = 0
        total_amount_tax8 = 0
        discount_amount = 0

        for sale_product_data in sale_products_data:
            product = self._get_product(sale_product_data["jan"])
            store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
            effective_price = store_price.get_price() if store_price else product.price
            discount = sale_product_data.get("discount", 0)
            quantity = sale_product_data.get("quantity", 1)

            # 値引き後の商品ごとの税込小計を計算
            subtotal_with_tax = (effective_price - discount) * quantity

            # 税率ごとに税込合計金額を求める
            if product.tax == 10:
                total_amount_tax10 += subtotal_with_tax
            elif product.tax == 8:
                total_amount_tax8 += subtotal_with_tax

            total_quantity += quantity
            discount_amount += discount * quantity

        # 税率ごとの合計金額に基づいて税額を計算
        total_tax10 = total_amount_tax10 * 10 / 110
        total_tax8 = total_amount_tax8 * 8 / 108
        total_tax = total_tax10 + total_tax8
        
        # 合計金額を計算
        total_amount = total_amount_tax10 + total_amount_tax8

        return {
            'total_quantity': total_quantity,
            'total_tax10': int(total_tax10),
            'total_tax8': int(total_tax8),
            'tax_amount': round(total_tax),
            'discount_amount': discount_amount,
            'total_amount': total_amount
        }

    def _create_payments(self, transaction_instance, payments_data):
        for payment_data in payments_data:
            Payment.objects.create(
                transaction=transaction_instance,
                payment_method=payment_data['payment_method'],
                amount=payment_data['amount']
            )

    def _validate_payments(self, total_payments, total_amount, payments_data):
        # 支払方法のバリデーション
        if total_payments < total_amount:
            raise serializers.ValidationError("支払いの合計金額が取引の合計金額を下回っています。")

        total_cashless_amount = sum(
            payment['amount'] for payment in payments_data if payment['payment_method'] != 'cash'
        )
        
        if total_cashless_amount > total_amount:
            raise serializers.ValidationError("現金以外の支払方法の総計が合計金額を超えています。")

    def _process_product(self, store_code, sale_product_data, status):
        product = self._get_product(sale_product_data["jan"])
        store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
        effective_price = store_price.get_price() if store_price else product.price

        # トレーニングモードでない場合、在庫を減らす
        if status != "training":
            stock = self._get_stock(store_code, product)
            stock.stock -= sale_product_data.get("quantity", 1)
            stock.save()

        return product, effective_price

    def _get_product(self, jan_code):
        try:
            return Product.objects.get(jan=jan_code)
        except Product.DoesNotExist:
            raise serializers.ValidationError(f"JANコード {jan_code} は登録されていません。")

    def _get_stock(self, store_code, product):
        try:
            return Stock.objects.get(store_code=store_code, jan=product)
        except Stock.DoesNotExist:
            raise serializers.ValidationError(
                f"店舗コード {store_code} と JANコード {product.jan} の在庫は登録されていません。"
            )

    def _create_transaction_details(self, transaction_instance, sale_products_data, status, store_code):
        for sale_product_data in sale_products_data:
            product, effective_price = self._process_product(store_code, sale_product_data, status)
            discount = sale_product_data.get("discount", 0)
            quantity = sale_product_data.get("quantity", 1)

            TransactionDetail.objects.create(
                transaction=transaction_instance,
                jan=product,
                name=product.name,
                price=effective_price,
                tax=product.tax,
                discount=discount,
                quantity=quantity
            )


class ProductSerializer(serializers.ModelSerializer):
    """商品情報のシリアライザー"""
    class Meta:
        model = Product
        fields = ['jan', 'name', 'price', 'tax', 'status']


class ProductVariationDetailSerializer(serializers.ModelSerializer):
    """商品バリエーション詳細のシリアライザー"""
    product = ProductSerializer()
    class Meta:
        model = ProductVariationDetail
        fields = ['product', 'color_name', 'product_variation']


class ProductVariationSerializer(serializers.ModelSerializer):
    """商品バリエーションのシリアライザー"""
    variations = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariation
        fields = ['instore_jan', 'name', 'variations']

    def get_variations(self, obj):
        """バリエーション詳細を取得するメソッド"""
        variation_details = ProductVariationDetail.objects.filter(
            product_variation=obj
        ).select_related('product')
        return ProductVariationDetailSerializer(variation_details, many=True).data


class StockSerializer(serializers.ModelSerializer):
    standard_price = serializers.IntegerField(source='jan.price')  # 商品の標準価格
    store_price = serializers.SerializerMethodField()  # 店舗価格
    tax = serializers.IntegerField(source='jan.tax')  # 税率を追加

    class Meta:
        model = Stock
        fields = ['store_code', 'jan', 'stock', 'standard_price', 'store_price', 'tax']
# TODO:なぜmodels側のget_priceだけでは価格が取得できないのか調査する
#（現在のコードだとstore_priceが存在しない時のフォールバック処理を二回行なっていることになっている）
    def get_store_price(self, obj):
        store_price = StorePrice.objects.filter(store_code=obj.store_code, jan=obj.jan).first()
        return store_price.get_price() if store_price else obj.jan.price  # StorePriceがない場合はProductの価格を返す


class StockReceiveItemSerializer(serializers.Serializer):
    jan = serializers.CharField(max_length=13, required=True)
    additional_stock = serializers.IntegerField(min_value=1, required=True)


class StockReceiveSerializer(serializers.Serializer):
    store_code = serializers.CharField(max_length=20, required=True)
    staff_code = serializers.CharField(max_length=6, required=True)
    items = StockReceiveItemSerializer(many=True)

    def validate(self, data):
        staff_code = data["staff_code"]
        permission_checker = UserPermissionChecker(staff_code)
        permissions = permission_checker.get_permissions()

        # 入荷権限のチェック
        if "stock_receive" not in permissions:
            raise serializers.ValidationError("入荷権限がありません。")

        # 入荷店舗とスタッフの所属店舗が異なる場合、global権限をチェック
        store_code = data["store_code"]
        staff = Staff.objects.get(staff_code=staff_code)
        if staff.affiliate_store.store_code != store_code:
            if "global" not in permissions:
                raise serializers.ValidationError("このスタッフは他店舗の在庫を操作できません。")

        return data


class StockReceiveHistoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockReceiveHistoryItem
        fields = ["additional_stock", "received_at", "staff_code"]


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["staff_code"] = user.staff_code

        return token
