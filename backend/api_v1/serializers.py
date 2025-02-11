from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from rest_framework.fields import empty
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, Customer, WalletTransaction, Wallet, Approval, Store
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


class TaxRateManager:
    @staticmethod
    def get_applied_tax(product, specified_tax_rate):
        # 指定がなければ、商品の元の税率を返す
        if specified_tax_rate is None:
            return product.tax
        # 指定税率は 0, 8, 10 のいずれかでなければならない
        if specified_tax_rate not in [0, 8, 10]:
            raise serializers.ValidationError(
                f"JAN:{product.jan} 不正な税率。税率は0, 8, 10のいずれかを指定してください。"
            )
        # 税率10%の商品は変更不可
        if product.tax == 10 and specified_tax_rate != 10:
            raise serializers.ValidationError(
                f"JAN:{product.jan} 税率10%の商品は税率変更できません。"
            )
        # 税率変更が禁止されている場合は、指定税率と元の税率が一致しなければならない
        if product.disable_change_tax:
            if specified_tax_rate != product.tax:
                raise serializers.ValidationError(
                    f"JAN:{product.jan} は税率の変更が禁止されています。"
                )
            return product.tax
        # 8%の商品について、指定が10%であれば変更を許可
        if product.tax == 8 and specified_tax_rate == 10:
            return 10
        # それ以外で、指定税率が元と異なる場合はエラー
        if specified_tax_rate != product.tax:
            raise serializers.ValidationError(
                f"JAN:{product.jan} は税率変更できません。"
            )
        return product.tax


class NonStopListSerializer(serializers.ListSerializer):
    def run_validation(self, data=empty):
        if data is empty:
            return []
        ret = []
        errors = []
        for index, item in enumerate(data):
            try:
                value = self.child.run_validation(item)
                ret.append(value)
                errors.append({})  # 正常なら空の辞書
            except serializers.ValidationError as exc:
                # 例外は捕捉して、エラー内容をリストに追加
                ret.append(item)
                errors.append(exc.detail)
        # 1件でもエラーがあれば、全件分のエラー情報をまとめて返す
        if any(error for error in errors if error):
            raise serializers.ValidationError(errors)
        return ret


class TransactionDetailSerializer(serializers.ModelSerializer):
    tax = serializers.IntegerField(required=False)
    discount = serializers.IntegerField(required=False, default=0)
    quantity = serializers.IntegerField(min_value=1)

    class Meta:
        model = TransactionDetail
        fields = ["jan", "name", "price", "tax", "discount", "quantity"]
        # name, price はシステム側で設定されるので read_only
        read_only_fields = ["name", "price"]
        # many=True時にカスタムListSerializerを利用
        list_serializer_class = NonStopListSerializer

    def validate(self, data):
        # --- 値引きの基本チェック（負の値はNG） ---
        discount = data.get('discount', 0)
        if discount < 0:
            raise serializers.ValidationError({"discount": "割引額は0以上である必要があります。"})

        # --- 商品の存在確認 ---
        jan = data.get('jan')
        try:
            product = Product.objects.get(jan=jan)
        except Product.DoesNotExist:
            raise serializers.ValidationError({"jan": f"JANコード {jan} は登録されていません。"})

        # --- 税率のバリデーション ---
        # クライアント側から tax が指定されていなければ、商品の税率を使用
        specified_tax_rate = data.get('tax')
        try:
            applied_tax_rate = TaxRateManager.get_applied_tax(product, specified_tax_rate)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"tax": e.detail if hasattr(e, "detail") else str(e)})

        data['tax'] = applied_tax_rate
        return data


class TransactionSerializer(serializers.ModelSerializer):
    sale_products = TransactionDetailSerializer(many=True)
    payments = PaymentSerializer(many=True)
    date = serializers.DateTimeField(required=False)
    approval_number = serializers.CharField(required=True, write_only=True)

    class Meta:
        model = Transaction
        fields = ["id", "status", "date", "store_code", "terminal_id", "staff_code", "approval_number", "user", "total_tax10", "total_tax8", "tax_amount", "discount_amount", "total_amount", "deposit", "change", "total_quantity", "payments", "sale_products"]
        read_only_fields = ["id", "user", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount", "deposit"]

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

    def validate(self, data):
        errors = {}

        staff_code = data.get("staff_code")
        store_code = data.get("store_code")
        payments_data = data.get("payments", [])
        sale_products_data = data.get("sale_products", [])

        # スタッフ権限のチェック
        try:
            permissions = self._check_permissions(staff_code, store_code)
        except serializers.ValidationError as e:
            errors["staff"] = e.detail
            permissions = None

        # ③ 承認番号のチェック
        approval_errors = {}
        approval_number = data.get("approval_number")
        if not approval_number:
            approval_errors["approval_number"] = "承認番号が入力されていません。"
        else:
            # 形式チェック：8桁の数字であるか
            if not (len(approval_number) == 8 and approval_number.isdigit()):
                approval_errors["approval_number"] = "承認番号の形式に誤りがあります。数字8桁を入力してください。"
            else:
                try:
                    approval = Approval.objects.get(approval_number=approval_number)
                    if approval.is_used:
                        approval_errors["approval_number"] = "承認番号は既に使用済みです。"
                except Approval.DoesNotExist:
                    approval_errors["approval_number"] = "承認番号が存在しません。"
        if approval_errors:
            errors.update(approval_errors)

        # 値引き（ディスカウント）のチェック（各商品ごとにエラーを集約）
        discount_errors = self._aggregate_discount_errors(sale_products_data, store_code, permissions)
        if discount_errors:
            errors["sale_products_discount"] = discount_errors

        # 支払い金額のチェック
        try:
            totals = self._calculate_totals(sale_products_data, store_code)
        except serializers.ValidationError as e:
            errors["sale_products_totals"] = e.detail
            totals = None

        if totals is not None:
            total_payments = sum(payment['amount'] for payment in payments_data)
            try:
                self._validate_payments(total_payments, totals['total_amount'], payments_data)
            except serializers.ValidationError as e:
                errors["payments"] = e.detail

        if errors:
            raise serializers.ValidationError(errors)

        return data

    def _aggregate_discount_errors(self, sale_products_data, store_code, permissions):
        discount_error_list = []
        for index, sale_product in enumerate(sale_products_data):
            try:
                # 個別の商品について、値引きチェックを実施
                self._discount_check([sale_product], store_code, permissions)
            except serializers.ValidationError as e:
                discount_error_list.append({f"product_{index}": e.detail})
        return discount_error_list

    def _discount_check(self, sale_products_data, store_code, permissions):
        for sale_product in sale_products_data:
            product = self._get_product(sale_product["jan"])
            discount = sale_product.get("discount", 0)
            if discount > 0:
                if product.disable_change_price:
                    raise serializers.ValidationError(f"JAN:{sale_product['jan']} の値引きは許可されていません。")
                store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
                effective_price = store_price.get_price() if store_price else product.price
                if discount > effective_price:
                    raise serializers.ValidationError(f"JAN:{sale_product['jan']} 不正な割引額が入力されました。")
                if discount < 0:
                    raise serializers.ValidationError(f"JAN:{sale_product['jan']} 割引額は0以上である必要があります。")

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
            subtotal_with_tax = (effective_price - discount) * quantity

            # 既に TransactionDetailSerializer で検証済みの tax を利用
            applied_tax_rate = sale_product_data.get("tax")
            if applied_tax_rate == 10:
                total_amount_tax10 += subtotal_with_tax
            else:
                total_amount_tax8 += subtotal_with_tax

            total_quantity += quantity
            discount_amount += discount * quantity

        total_tax10 = total_amount_tax10 * 10 / 110
        total_tax8 = total_amount_tax8 * 8 / 108
        total_tax = total_tax10 + total_tax8
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
            applied_tax_rate = sale_product_data.get("tax")

            TransactionDetail.objects.create(
                transaction=transaction_instance,
                jan=product,
                name=product.name,
                price=effective_price,
                tax=applied_tax_rate,
                discount=discount,
                quantity=quantity
            )

    def create(self, validated_data):
        sale_products_data = validated_data.pop("sale_products", [])
        payments_data = validated_data.pop("payments", [])
        staff_code = validated_data.pop("staff_code", None)
        store_code = validated_data.pop("store_code", None)
        status = validated_data.get("status")
        terminal_id = validated_data.pop("terminal_id", None)

        # 承認番号の取得（validate() でチェック済み）
        approval_number = validated_data.pop("approval_number", None)
        try:
            approval = Approval.objects.get(approval_number=approval_number)
            # トレーニングモードでない場合のみ、既に使用済みかどうかのチェックを行う
            if status != "training" and approval.is_used:
                raise serializers.ValidationError("承認番号は既に使用済みです。")
            user = approval.user
        except Approval.DoesNotExist:
            raise serializers.ValidationError("無効または期限切れの承認番号です。")

        totals = self._calculate_totals(sale_products_data, store_code)
        total_payments = sum(payment['amount'] for payment in payments_data)

        with transaction.atomic():
            # ウォレット支払いのバリデーション
            wallet = user.wallet
            wallet_payments = [p for p in payments_data if p['payment_method'] == 'wallet']
            if wallet_payments:
                total_wallet_payment = sum(p['amount'] for p in wallet_payments)
                if total_wallet_payment > wallet.balance:
                    shortage = total_wallet_payment - wallet.balance
                    raise serializers.ValidationError(f"ウォレット残高不足。{int(shortage)}円分不足しています。")

            transaction_instance = Transaction.objects.create(
                date=timezone.now(),
                user=user,  # 承認番号から取得したユーザーを設定
                terminal_id=terminal_id,
                staff_code=staff_code,
                store_code=store_code,
                deposit=total_payments,
                change=total_payments - totals['total_amount'],
                total_quantity=totals['total_quantity'],
                total_tax10=totals['total_tax10'],
                total_tax8=totals['total_tax8'],
                tax_amount=totals['tax_amount'],
                discount_amount=totals['discount_amount'],
                total_amount=totals['total_amount'],
                **validated_data
            )

            if wallet_payments:
                wallet.withdraw(total_wallet_payment, transaction=transaction_instance)

            self._create_transaction_details(transaction_instance, sale_products_data, status, store_code)
            self._create_payments(transaction_instance, payments_data)

        return transaction_instance


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


class WalletChargeSerializer(serializers.Serializer):
    user_id = serializers.CharField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)

    def validate_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("入金額は0以上である必要があります。")
        if value > 10000:
            raise serializers.ValidationError("一度にチャージできる金額は10000円までです。")
        if value % 100 != 0:
            raise serializers.ValidationError("チャージは100円単位で行う必要があります。")
        return value

    def validate_user_id(self, value):
        if not CustomUser.objects.filter(id=value).exists():
            raise serializers.ValidationError("指定されたユーザーは存在しません。")
        return value

    def create_wallet_transaction(self, user, amount):
        # ウォレットが存在しない場合は新規作成
        wallet, created = Wallet.objects.get_or_create(user=user, defaults={'balance': 0})

        # amountが0の場合は新規作成のみ
        if created and amount == 0:
            return wallet.balance, created

        # 既存のウォレットに入金処理を行う
        wallet.deposit(amount)
        return wallet.balance, created


class WalletBalanceSerializer(serializers.Serializer):
    user_id = serializers.CharField()

    def validate_user_id(self, value):
        if not CustomUser.objects.filter(id=value).exists():
            raise serializers.ValidationError("指定されたユーザーは存在しません。")
        return value


class StockReceiveHistoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockReceiveHistoryItem
        fields = ["additional_stock", "received_at", "staff_code"]


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    # 既存の JWT 発行エンドポイント用（メールアドレス/パスワード認証用）のシリアライザー
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["staff_code"] = getattr(user, "staff_code", None)
        return token

class CustomUserTokenSerializer(serializers.Serializer):
    """カスタムユーザー用のトークンシリアライザー（custom-token エンドポイント用）"""
    # こちらは出力用のシンプルなシリアライザー。入力値は特になし。
    refresh = serializers.CharField(required=False)
    access = serializers.CharField(required=False)

class ApprovalSerializer(serializers.ModelSerializer):
    """承認番号モデル用のシリアライザー"""
    class Meta:
        model = Approval
        fields = ['id', 'user', 'approval_number', 'created_at']