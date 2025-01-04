from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice
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


class TransactionDetailSerializer(serializers.ModelSerializer):
    jan = serializers.CharField()  # JANコードを受け取る
    discount = serializers.IntegerField(required=False, default=0)  # 割引額を整数として受け取る

    class Meta:
        model = TransactionDetail
        fields = ["jan", "discount", "quantity"]  # 必要なフィールドを指定


class TransactionSerializer(serializers.ModelSerializer):
    sale_products = TransactionDetailSerializer(many=True)
    date = serializers.DateTimeField(required=False)

    class Meta:
        model = Transaction
        fields = ["id", "status", "date", "store_code", "terminal_id", "staff_code", "total_tax10", "total_tax8", "tax_amount", "total_amount", "discount_amount", "deposit", "change", "total_quantity", "sale_products"]
        read_only_fields = ["id", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount"]

    def validate_status(self, value):
        if value == "return":
            raise serializers.ValidationError("このステータスは選択できません。")
        return value

    def validate_deposit(self, value):
        if value <= 0:
            raise serializers.ValidationError("預かり金は正の値である必要があります。")
        return value

    def validate_sale_products(self, value):
        if not value:
            raise serializers.ValidationError("少なくとも1つの商品を提供する必要があります。")
        return value

    def create(self, validated_data):
        sale_products_data = validated_data.pop("sale_products")
        deposit = validated_data.get("deposit")
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
            transaction_instance = self._create_transaction(validated_data)

            for sale_product_data in sale_products_data:
                jan_code = sale_product_data["jan"]
                quantity = sale_product_data.get("quantity", 1)
                discount = sale_product_data.get("discount", 0)

                product = self._get_product(jan_code)
                stock = self._get_stock(store_code, product)

                # StorePriceを取得
                store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()

                # 店舗価格を取得
                effective_price = store_price.get_price() if store_price else product.price

                # トレーニングモードの場合在庫を減らさない
                if status == "training":
                    # 在庫を減らさず、通常の処理を続ける
                    pass
                else:
                    stock.stock -= quantity
                    stock.save()

                # トランザクション詳細を作成（effective_priceを使用）
                self._create_transaction_detail(transaction_instance, product, effective_price, discount, quantity)

                # 税金計算
                total_tax10, total_tax8 = self._calculate_tax(product, discount, quantity, total_tax10, total_tax8)

                # total_amountを計算
                total_amount += (effective_price - discount) * quantity  # effective_priceを使用した計算

                total_quantity += quantity
                discount_amount += discount * quantity

            # 合計を計算
            self._calculate_totals(
                transaction_instance, total_tax10, total_tax8, discount_amount, deposit, total_quantity, total_amount
            )

            # 取引を保存
            transaction_instance.save()

        return transaction_instance

        # 通常の処理（在庫の減少とデータベースへのコミット）
        with transaction.atomic():
            transaction_instance = self._create_transaction(validated_data)

            for sale_product_data in sale_products_data:
                jan_code = sale_product_data["jan"]
                quantity = sale_product_data.get("quantity", 1)
                discount = sale_product_data.get("discount", 0)

                product = self._get_product(jan_code)
                stock = self._get_stock(store_code, product)

                # StorePriceを取得
                store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()

                # 店舗価格を取得
                effective_price = store_price.get_price() if store_price else product.price

                # 在庫を減らす
                stock.stock -= quantity
                stock.save()

                # トランザクション詳細を作成（effective_priceを使用）
                self._create_transaction_detail(transaction_instance, product, effective_price, discount, quantity)

                # 税金計算
                total_tax10, total_tax8 = self._calculate_tax(product, discount, quantity, total_tax10, total_tax8)

                # total_amountを計算
                total_amount += (effective_price - discount) * quantity  # effective_priceを使用した計算

                total_quantity += quantity
                discount_amount += discount * quantity

            # 合計を計算
            self._calculate_totals(
                transaction_instance, total_tax10, total_tax8, discount_amount, deposit, total_quantity, total_amount
            )

            # 取引を保存
            transaction_instance.save()

        return transaction_instance

    def _create_transaction(self, validated_data):
        current_time = timezone.now()
        return Transaction.objects.create(
            date=current_time, **validated_data, total_quantity=0, total_tax10=0, total_tax8=0, tax_amount=0, total_amount=0, change=0, discount_amount=0,
        )

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
        """トランザクション詳細を作成（effective_priceを使用）"""
        TransactionDetail.objects.create(
            transaction=transaction_instance,
            jan=product,  # Productインスタンスを直接渡す
            name=product.name,
            price=effective_price,  # effective_priceを使用
            tax=product.tax,
            discount=discount,
            quantity=quantity,
        )

    def _calculate_tax(self, product, discount, quantity, total_tax10, total_tax8):
        if product.tax == 10:
            total_tax10 += (product.price - discount) * quantity
        elif product.tax == 8:
            total_tax8 += (product.price - discount) * quantity
        return total_tax10, total_tax8

    def _calculate_totals(
        self, transaction_instance, total_tax10, total_tax8, discount_amount, deposit, total_quantity, total_amount
    ):
        transaction_instance.total_tax10 = total_tax10 * 10 // 110  # 10%税額
        transaction_instance.total_tax8 = total_tax8 * 8 // 108  # 8%税額
        transaction_instance.tax_amount = transaction_instance.total_tax10 + transaction_instance.total_tax8
        transaction_instance.total_amount = total_amount  # effective_priceを使用した合計
        transaction_instance.discount_amount = discount_amount
        transaction_instance.change = deposit - transaction_instance.total_amount
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
# （現在のコードだとstore_priceが存在しない時のフォールバック処理を二回行なっていることになっている）
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

        # スタッフの権限チェック
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
