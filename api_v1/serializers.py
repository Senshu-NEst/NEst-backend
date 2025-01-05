from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice, Payment
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class UserPermissionChecker:
    def __init__(self, staff_code):
        self.staff_code = staff_code

    def get_permissions(self):
        """スタッフの権限を取得して、権限リストを返す"""
        try:
            user = CustomUser.objects.select_related("permission").get(staff_code=self.staff_code)

            if not user.is_active:
                raise serializers.ValidationError("このスタッフは現在無効です。")

            permissions = []
            if user.is_superuser:
                permissions.append("superuser")
            if not user.permission:
                raise serializers.ValidationError("このスタッフに権限が設定されていません。")

            # 権限の追加
            permission_fields = [
                ("register_permission", "register"),
                ("global_permission", "global"),
                ("change_price_permission", "change_price"),
                ("stock_receive_permission", "stock_receive"),
            ]

            for field, name in permission_fields:
                if getattr(user.permission, field):
                    permissions.append(name)

            return permissions

        except CustomUser.DoesNotExist:
            raise serializers.ValidationError("指定されたスタッフが存在しません。")


class PaymentSerializer(serializers.ModelSerializer):
    payment_method = serializers.ChoiceField(choices=Payment.PAYMENT_METHOD_CHOICES)
    amount = serializers.IntegerField()

    class Meta:
        model = Payment
        fields = ['payment_method', 'amount']


class TransactionDetailSerializer(serializers.ModelSerializer):
    jan = serializers.CharField()  # JANコードを受け取る
    discount = serializers.IntegerField(required=False, default=0)

    class Meta:
        model = TransactionDetail
        fields = ["jan", "discount", "quantity"]


class TransactionSerializer(serializers.ModelSerializer):
    sale_products = TransactionDetailSerializer(many=True)
    payments = PaymentSerializer(many=True)
    date = serializers.DateTimeField(required=False)

    class Meta:
        model = Transaction
        fields = ["id", "status", "date", "store_code", "terminal_id", "staff_code", "total_tax10", "total_tax8", "tax_amount", "discount_amount", "total_amount", "deposit", "change", "total_quantity", "payments", "sale_products"]
        read_only_fields = ["id", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount", "deposit"]

    def validate_status(self, value):
        if value == "return":
            raise serializers.ValidationError("無効なstatusが指定されました。")
        return value


    def validate_sale_products(self, value):
        if not value:
            raise serializers.ValidationError("")
        return value

    def validate_payments(self, value):
        if not value:
            raise serializers.ValidationError("支払方法を登録してください。")

        total_cashless_amount = sum(
            payment['amount'] for payment in value if payment['payment_method'] != 'cash'
        )
        total_amount = self.initial_data.get('total_amount', 0)

        if total_cashless_amount > total_amount:
            raise serializers.ValidationError("現金以外の支払方法の総計が合計金額を超えています。")

        total_payments = sum(payment['amount'] for payment in value)
        if total_payments < total_amount:
            raise serializers.ValidationError("支払いの合計金額が取引の合計金額を下回っています。")

        return value

    def create(self, validated_data):
        sale_products_data = validated_data.pop("sale_products")
        payments_data = validated_data.pop("payments")
        staff_code = validated_data.get("staff_code")
        store_code = validated_data["store_code"].store_code
        status = validated_data.get("status")

        # 権限のチェック
        permission_checker = UserPermissionChecker(staff_code)
        permissions = permission_checker.get_permissions()

        # 所属店舗を取得
        user = CustomUser.objects.get(staff_code=staff_code)

        # register_permissionのチェック
        if "register" not in permissions:
            raise serializers.ValidationError("このスタッフは販売を行う権限がありません。")

        # staffの所属店舗とPOSTされたstore_codeを確認
        if user.affiliate_store.store_code != store_code:
            if "global" not in permissions:
                raise serializers.ValidationError("このスタッフは自店のみ処理可能です。")

        # 値引きがある場合の権限チェック
        for sale_product in sale_products_data:
            if sale_product.get("discount", 0) != 0 and "change_price" not in permissions:
                raise serializers.ValidationError("このスタッフは売価変更を行う権限がありません。")

        total_quantity, total_tax10, total_tax8, discount_amount = 0, 0, 0, 0
        total_amount = 0

        with transaction.atomic():
            # depositの合計を計算
            total_payments = sum(payment['amount'] for payment in payments_data)
            
            transaction_instance = Transaction.objects.create(
                date=timezone.now(), **validated_data, deposit=total_payments, total_quantity=0, total_tax10=0, total_tax8=0, tax_amount=0, total_amount=0, change=0, discount_amount=0,
            )

            for sale_product_data in sale_products_data:
                product, stock, effective_price = self._process_product(store_code, sale_product_data, status)
                self._create_transaction_detail(
                    transaction_instance, product, effective_price,
                    sale_product_data.get("discount", 0), sale_product_data.get("quantity", 1)
                )

                total_tax10, total_tax8 = self._calculate_tax(
                    product, sale_product_data.get("discount", 0),
                    sale_product_data.get("quantity", 1), total_tax10, total_tax8
                )

                total_amount += (effective_price - sale_product_data.get("discount", 0)) * sale_product_data.get("quantity", 1)
                total_quantity += sale_product_data.get("quantity", 1)
                discount_amount += sale_product_data.get("discount", 0) * sale_product_data.get("quantity", 1)

            for payment_data in payments_data:
                Payment.objects.create(
                    transaction=transaction_instance,
                    payment_method=payment_data['payment_method'],
                    amount=payment_data['amount']
                )

            self._calculate_totals(
                transaction_instance, total_tax10, total_tax8,
                discount_amount, total_payments, total_quantity, total_amount
            )
            transaction_instance.save()

        return transaction_instance

    def _create_transaction(self, validated_data):
        return Transaction.objects.create(
            date=timezone.now(), **validated_data, total_quantity=0, total_tax10=0, total_tax8=0, tax_amount=0, total_amount=0, change=0, discount_amount=0,
        )

    def _process_product(self, store_code, sale_product_data, status):
        jan_code = sale_product_data["jan"]
        product = self._get_product(jan_code)
        stock = self._get_stock(store_code, product)

        store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
        effective_price = store_price.get_price() if store_price else product.price

        if status != "training":
            stock.stock -= sale_product_data.get("quantity", 1)
            stock.save()

        return product, stock, effective_price

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

    def _create_transaction_detail(self, transaction_instance, product, effective_price, discount, quantity):
        TransactionDetail.objects.create(
            transaction=transaction_instance, jan=product, name=product.name, price=effective_price, tax=product.tax, discount=discount, quantity=quantity,
        )

    def _calculate_tax(self, product, discount, quantity, total_tax10, total_tax8):
        if product.tax == 10:
            total_tax10 += (product.price - discount) * quantity
        elif product.tax == 8:
            total_tax8 += (product.price - discount) * quantity
        return total_tax10, total_tax8

    def _calculate_totals(self, transaction_instance, total_tax10, total_tax8, discount_amount, deposit, total_quantity, total_amount):
        transaction_instance.total_tax10 = total_tax10 * 10 // 110
        transaction_instance.total_tax8 = total_tax8 * 8 // 108
        transaction_instance.tax_amount = transaction_instance.total_tax10 + transaction_instance.total_tax8
        transaction_instance.total_amount = total_amount
        transaction_instance.discount_amount = discount_amount
        transaction_instance.change = deposit - total_amount
        transaction_instance.total_quantity = total_quantity


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = "__all__"


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
        staff = CustomUser.objects.get(staff_code=staff_code)
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
