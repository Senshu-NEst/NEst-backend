from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from rest_framework.fields import empty
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, Customer, WalletTransaction, Wallet, Approval, Store, ReturnTransaction, ReturnDetail, ReturnPayment
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
                ("void_permission", "void"),
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


class BaseTransactionSerializer(serializers.ModelSerializer):
    def _check_permissions(self, staff_code, store_code, required_permissions=None):
        if not staff_code:
            raise serializers.ValidationError("スタッフコードが指定されていません。")
        try:
            staff = Staff.objects.get(staff_code=staff_code)
        except Staff.DoesNotExist:
            raise serializers.ValidationError("指定されたスタッフが存在しません。")

        permission_checker = UserPermissionChecker(staff_code)
        permissions = permission_checker.get_permissions()

        # store_codeがオブジェクトの場合は、store_codeを取り出す
        if isinstance(store_code, Store):
            store_code = store_code.store_code

        if staff.affiliate_store.store_code != store_code:
            if "global" not in permissions:
                raise serializers.ValidationError("このスタッフは自店のみ処理可能です。")
        # 必要なパーミッションの存在確認
        if required_permissions:
            missing_permissions = [perm for perm in required_permissions if perm not in permissions]
            if missing_permissions:
                raise serializers.ValidationError(f"このスタッフは次の権限が不足しています: {', '.join(missing_permissions)}")
        return permissions


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
        list_serializer_class = NonStopListSerializer

    def validate(self, data):
        # ※ここでは、商品存在チェックおよび税率の検証のみを実施し、
        #   値引きのチェック（権限や実効価格との比較）は TransactionSerializer 側に統一する
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


class TransactionSerializer(BaseTransactionSerializer):
    # 外部から不足分を補完するため、original_transaction をオプション入力とする
    original_transaction = serializers.PrimaryKeyRelatedField(
        queryset=Transaction.objects.all(), write_only=True, required=False
    )
    sale_products = TransactionDetailSerializer(many=True)
    payments = PaymentSerializer(many=True)
    date = serializers.DateTimeField(required=False)
    # 承認番号は外部入力がない場合もあるので required=False
    approval_number = serializers.CharField(required=False, write_only=True)

    class Meta:
        model = Transaction
        fields = ["id", "status", "date", "store_code", "terminal_id", "staff_code", "approval_number", "user", "total_tax10", "total_tax8", "tax_amount", "discount_amount", "total_amount", "deposit", "change", "total_quantity", "payments", "sale_products", "original_transaction"]
        read_only_fields = [ "id", "user", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount", "deposit"]

    def __init__(self, *args, **kwargs):
        """
        コンテキストに 'external_data': True がある場合、read_only_fields も外部から上書きできるようにする。
        """
        super().__init__(*args, **kwargs)
        if self.context.get("external_data", False):
            for field_name in self.Meta.read_only_fields:
                if field_name in self.fields:
                    self.fields[field_name].read_only = False

    def validate_status(self, value):
        valid_statuses = ["sale", "training"]
        if value not in valid_statuses:
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

        # スタッフコードのチェックおよび権限チェック
        staff_code = data.get("staff_code")
        try:
            permissions = self._check_permissions(staff_code, data.get("store_code"), required_permissions=["register"])
        except serializers.ValidationError as e:
            errors["staff"] = e.detail
            permissions = None

        # 承認番号のチェック（external_dataがFalseの場合のみ）
        if not self.context.get("external_data", False):
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

        # 各商品の値引きチェック（各商品ごとにエラーを集約）
        sale_products_data = data.get("sale_products", [])
        discount_errors = self._aggregate_discount_errors(sale_products_data, data.get("store_code"), permissions)
        if discount_errors:
            errors["sale_products_discount"] = discount_errors

        # 支払い金額のチェック
        payments_data = data.get("payments", [])
        try:
            totals = self._calculate_totals(sale_products_data, data.get("store_code"))
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
        for sale_product in sale_products_data:
            jan = sale_product.get("jan", "不明")
            try:
                self._discount_check(sale_product, store_code, permissions)
            except serializers.ValidationError as e:
                discount_error_list.append({f"JAN:{jan}": e.detail})
        return discount_error_list

    def _discount_check(self, sale_product, store_code, permissions):
        jan = sale_product.get("jan", "不明")
        discount = sale_product.get("discount", 0)
        # スタッフに売価変更権限がない場合のチェック
        if discount != 0:
            if permissions is None or "change_price" not in permissions:
                raise serializers.ValidationError(f"JAN:{jan} このスタッフは売価変更を行う権限がありません。")
        product = self._get_product(jan)
        if discount > 0 and product.disable_change_price:
            raise serializers.ValidationError(f"JAN:{jan} の値引きは許可されていません。")
        store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
        effective_price = store_price.get_price() if store_price else product.price
        if discount > effective_price:
            raise serializers.ValidationError(f"JAN:{jan} 不正な割引額が入力されました。")
        if discount < 0:
            raise serializers.ValidationError(f"JAN:{jan} 割引額は0以上である必要があります。")

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
            applied_tax_rate = sale_product_data.get("tax")
            if applied_tax_rate == 10:
                total_amount_tax10 += subtotal_with_tax
            else:
                total_amount_tax8 += subtotal_with_tax
            total_quantity += quantity
            discount_amount += discount * quantity

        total_tax10 = int(total_amount_tax10 * 10 / 110)
        total_tax8 = int(total_amount_tax8 * 8 / 108)
        total_tax = total_tax10 + total_tax8
        print(f"total10:{total_amount_tax10}")
        print(f"total8:{total_amount_tax8}")
        total_amount = total_amount_tax10 + total_amount_tax8

        return {
            'total_quantity': total_quantity,
            'total_tax10': total_tax10,
            'total_tax8': total_tax8,
            'tax_amount': total_tax,
            'discount_amount': discount_amount,
            'total_amount': total_amount
        }

    def _validate_payments(self, total_payments, total_amount, payments_data):
        if total_payments < total_amount:
            print(f"支払合計{total_payments}")
            print(f"合計金額{total_amount}")
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

    def _create_payments(self, transaction_instance, payments_data):
        payment_instances = []
        for payment_data in payments_data:
            payment_instance = Payment.objects.create(
                transaction=transaction_instance,
                payment_method=payment_data['payment_method'],
                amount=payment_data['amount']
            )
            payment_instances.append(payment_instance)

        # payments フィールドに関連付け
        transaction_instance.payments.set(payment_instances)

    def _create_transaction_details(self, transaction_instance, sale_products_data, status, store_code):
        product_map = {}

        # 商品を集約
        for sale_product_data in sale_products_data:
            jan = sale_product_data["jan"]
            tax = sale_product_data.get("tax")
            discount = sale_product_data.get("discount", 0)
            quantity = sale_product_data.get("quantity", 1)

            # 複合キーを作成
            product_key = (jan, tax, discount)

            if product_key not in product_map:
                product_map[product_key] = {
                    "quantity": 0,
                    "data": sale_product_data  # 元のデータを保持
                }
            product_map[product_key]["quantity"] += quantity

        # 合計した商品情報を基にトランザクション詳細を作成
        for product_key, product_info in product_map.items():
            jan, tax, discount = product_key
            quantity = product_info["quantity"]
            
            # 商品情報を取得する処理を一度だけ行う
            product = self._get_product(jan)
            store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
            effective_price = store_price.get_price() if store_price else product.price

            TransactionDetail.objects.create(
                transaction=transaction_instance,
                jan=product,
                name=product.name,
                price=effective_price,
                tax=tax,
                discount=discount,
                quantity=quantity
            )

    def create(self, validated_data):
        # まず、sale_products と payments は常に pop する
        sale_products_data = validated_data.pop("sale_products", [])
        payments_data = validated_data.pop("payments", [])

        # original_transaction が指定されている場合、欠落項目を補完
        original_transaction = validated_data.pop('original_transaction', None)
        if original_transaction:
            # 修正会計の場合は、ユーザーを元取引から取得する
            validated_data.setdefault('user', original_transaction.user)
            
            # external_data が True の場合は、ステータスを「再売」に設定
            if self.context.get("external_data", False):
                validated_data['status'] = "resale"  # ステータスを再売に設定
            else:
                validated_data.setdefault('status', original_transaction.status)

        # 通常会計の場合は承認番号のチェックを行う
        if not self.context.get("external_data", False):
            approval_number = validated_data.pop("approval_number", None)
            try:
                approval = Approval.objects.get(approval_number=approval_number)
                if validated_data.get("status") != "training" and approval.is_used:
                    raise serializers.ValidationError("承認番号は既に使用済みです。")
                validated_data["user"] = approval.user
            except Approval.DoesNotExist:
                raise serializers.ValidationError("無効または期限切れの承認番号です。")

        # いずれの場合も必須の計算処理を実施
        store_code = validated_data.get("store_code")
        totals = self._calculate_totals(sale_products_data, store_code)
        total_payments = sum(payment['amount'] for payment in payments_data)

        # 日付は常に現在時刻で設定
        validated_data['date'] = timezone.now()

        # 計算結果を各必須フィールドにセット
        validated_data.update({
            'deposit': total_payments,
            'change': total_payments - totals['total_amount'],
            'total_quantity': totals['total_quantity'],
            'total_tax10': totals['total_tax10'],
            'total_tax8': totals['total_tax8'],
            'tax_amount': totals['tax_amount'],
            'discount_amount': totals['discount_amount'],
            'total_amount': totals['total_amount'],
        })

        # トレーニングモード以外の場合はウォレット処理
        if validated_data.get("status") != "training":
            user = validated_data.get("user")
            wallet_payments = [p for p in payments_data if p['payment_method'] == 'wallet']
            if wallet_payments:
                total_wallet_payment = sum(p['amount'] for p in wallet_payments)
                if total_wallet_payment > user.wallet.balance:
                    shortage = total_wallet_payment - user.wallet.balance
                    raise serializers.ValidationError(f"ウォレット残高不足。{int(shortage)}円分不足しています。")
                user.wallet.withdraw(total_wallet_payment, transaction=None)

        # ここでは、external_data に関係なく通常の計算結果を使ってインスタンスを作成
        transaction_instance = Transaction.objects.create(**validated_data)
        self._create_transaction_details(transaction_instance, sale_products_data, validated_data.get("status"), store_code)
        self._create_payments(transaction_instance, payments_data)

        # 修正会計の場合は承認番号の更新をスキップする
        if not self.context.get("external_data", False) and validated_data.get("status") != "training":
            approval.is_used = True
            approval.save()

        return transaction_instance


class ProductSerializer(serializers.ModelSerializer):
    """商品情報のシリアライザー"""
    class Meta:
        model = Product
        fields = ['jan', 'name', 'price', 'tax', 'status', 'disable_change_price', 'disable_change_tax']


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
    standard_price = serializers.IntegerField(source='jan.price')
    store_price = serializers.SerializerMethodField()
    tax = serializers.IntegerField(source='jan.tax')
    name = serializers.CharField(source='jan.name', read_only=True)

    class Meta:
        model = Stock
        fields = ['store_code', 'name', 'jan', 'stock', 'standard_price', 'store_price', 'tax']
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
        staff_code = data.get("staff_code")
        store_code = data.get("store_code")
        try:
            BaseTransactionSerializer._check_permissions(self, staff_code, store_code, required_permissions=["stock_receive"])
        except serializers.ValidationError as e:
            error_message = e.detail if isinstance(e.detail, str) else ', '.join(e.detail)
            raise serializers.ValidationError({"staff": error_message})
        return data


class StockReceiveHistoryItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockReceiveHistoryItem
        fields = ["additional_stock", "received_at", "staff_code"]


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


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    # 既存の JWT 発行エンドポイント用（メールアドレス/パスワード認証用）のシリアライザー
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
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


class ReturnDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnDetail
        fields = ['jan', 'name', 'price', 'tax', 'discount', 'quantity']


class ReturnPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnPayment
        fields = ['payment_method', 'amount']


class ReturnTransactionSerializer(BaseTransactionSerializer):
    return_type = serializers.CharField()
    reason = serializers.CharField()
    # 支払いは、金額の符号で返金（負）と追加領収（正）を区別
    payments = ReturnPaymentSerializer(many=True, write_only=True)
    return_payments = ReturnPaymentSerializer(many=True, read_only=True)
    date = serializers.DateTimeField(source='created_at', read_only=True)
    # 返品時は、購入店舗と異なる可能性があるため必須
    store_code = serializers.CharField(required=True)
    terminal_id = serializers.CharField(required=True)
    return_products = ReturnDetailSerializer(many=True, source='return_details', read_only=True)
    additional_items = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField(required=True)),
        required=False
    )
    delete_items = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField(required=True)),
        required=False
    )

    class Meta:
        model = ReturnTransaction
        fields = ['id', 'origin_transaction', 'date', 'return_type', 'store_code', 'staff_code', 'terminal_id', 'reason', 'restock', 'payments', 'return_payments', 'return_products', 'additional_items', 'delete_items']
        extra_kwargs = {
            'origin_transaction': {'required': True},
            'staff_code': {'required': True},
            'restock': {'required': True},
        }

    def validate(self, data):
        errors = {}
        origin = data.get('origin_transaction')
        if not origin:
            errors['origin_transaction'] = "元取引IDが必要です。"
        elif origin.status != 'sale':
            errors['origin_transaction'] = "返品対象外取引です。"

        # スタッフ権限チェック
        staff_code = data.get("staff_code")
        try:
            self._check_permissions(staff_code, store_code=data.get('store_code'), required_permissions=["register", "void"])
        except serializers.ValidationError as e:
            errors["staff"] = e.detail

        # return_type のバリデーション（モデルの選択肢に限定）
        if data.get('return_type') not in dict(ReturnTransaction.RETURN_TYPE_CHOICES):
            errors['return_type'] = "不正なreturn_type"

        # partial返品の場合、追加・削除商品のいずれかが必須
        if data.get('return_type') == 'partial':
            additional = data.get('additional_items', [])
            delete = data.get('delete_items', [])
            if not additional and not delete:
                errors['additional_items'] = "一部返品の場合、追加商品の明細または削除商品の明細のいずれかは必要です。"
            # 項目の基本構造の検証
            self._validate_item_details(data)

            # 追加・削除商品の合計金額を共通処理で算出（値引きを考慮）
            additional_total, delete_total, net_amount = self._compute_additional_delete_totals(
                data.get('additional_items', []), data.get('delete_items', []), origin
            )
            payments = data.get('payments', [])
            payments_sum = sum(float(p.get('amount', 0)) for p in payments)

            # 各ケースでの支払い検証（変更せず）
            if net_amount > 0:
            # 追加領収発生：既収金は (元取引合計 + 削除商品合計) とみなし、追加領収は (追加合計 - 既収金)
                expected_additional = additional_total - delete_total
                if abs(payments_sum - expected_additional) > 0.01:
                    errors['payments'] = f"追加領収金の合計が不正です。必要値: {expected_additional}, 入力値: {payments_sum}"
            elif net_amount < 0:
                expected_refund = net_amount  # net_amount は負の値
                if abs(payments_sum - expected_refund) > 0.01:
                    errors['payments'] = f"返金金額の合計が不正です。必要値: {expected_refund}, 入力値: {payments_sum}"
            else:
                if abs(payments_sum) > 0.01:
                    errors['payments'] = f"返金も追加領収も発生しないはずですが、支払い合計: {payments_sum}"

        else:
            # 全返品の場合、返金金額は元取引合計と一致する必要がある
            payments = data.get('payments', [])
            sum_payments = sum(float(item.get('amount', 0)) for item in payments)
            if origin and sum_payments != origin.total_amount:
                errors['payments'] = "返金金額の合計が元取引の合計金額と一致しません。"

        if errors:
            raise serializers.ValidationError(errors)
        return data

    def _validate_item_details(self, data):
        # 各グループの明細について、必須項目と型変換を実施
        for group in ['additional_items', 'delete_items']:
            items = data.get(group, [])
            for item in items:
                if 'jan' not in item or 'quantity' not in item:
                    raise serializers.ValidationError(f"{group}にはJANコードと数量が必須です。")
                try:
                    item['quantity'] = int(item['quantity'])
                except ValueError:
                    raise serializers.ValidationError(f"{group}の数量は整数でなければなりません。")
                for field in ['discount', 'price']:
                    if field in item:
                        try:
                            item[field] = float(item[field])
                        except ValueError:
                            raise serializers.ValidationError(f"{group}の{field}は数値でなければなりません。")

    def _compute_additional_delete_totals(self, additional_items, delete_items, origin):
        """
        追加商品と削除商品の合計金額（値引き考慮）およびその差(net_amount)を算出する。
        追加商品の実質価格 = (price - discount) * quantity
        削除商品の実質価格は、元取引の販売明細から取得（price - discount）
        """
        additional_total = sum(
            (float(Product.objects.filter(jan=item.get('jan')).first().price) - float(item.get('discount', 0))) * int(item.get('quantity', 1))
            for item in additional_items if Product.objects.filter(jan=item.get('jan')).exists()
        )
        delete_total = sum(
            (detail.price - detail.discount) * int(item.get('quantity', 1))
            for item in delete_items
            for detail in origin.sale_products.all()
            if detail.jan.jan == item.get('jan')
        )
        net_amount = additional_total - delete_total
        return additional_total, delete_total, net_amount

    @transaction.atomic
    def create(self, validated_data):
        return_type = validated_data.pop('return_type')
        origin = validated_data['origin_transaction']
        # 返品対象の元取引はステータスを "return" に更新
        origin.status = 'return'
        origin.save()

        # store_code を Store インスタンスに変換
        store_instance = Store.objects.get(store_code=validated_data.pop('store_code'))

        ret_trans = ReturnTransaction.objects.create(
            origin_transaction=origin,
            staff_code=validated_data.get('staff_code'),
            return_type=return_type,
            reason=validated_data.get('reason'),
            restock=validated_data.get('restock'),
            store_code=store_instance,
            terminal_id=validated_data['terminal_id']
        )

        self._process_payments(ret_trans, origin, validated_data.get('payments', []))

        if validated_data.get('restock', False):
            self._restock_inventory(ret_trans, origin)

        if return_type == 'all':
            self._copy_sale_products(origin, ret_trans)
        elif return_type == 'partial':
            new_trans = self._process_partial_return(validated_data, ret_trans, origin)
            # 修正会計の取引IDを modify_id として記録
            ret_trans.modify_id = new_trans
            ret_trans.save()

        return ret_trans

    def _process_payments(self, ret_trans, origin, payments_data):
        # 支払い情報のうち、金額が負の場合は返金処理を実施
        for pay in payments_data:
            amount = float(pay.get('amount', 0))
            if amount < 0:
                refund_amt = abs(amount)
                ReturnPayment.objects.create(
                    return_transaction=ret_trans,
                    payment_method=pay.get('payment_method'),
                    amount=refund_amt
                )
                if pay.get('payment_method') == 'wallet':
                    wallet = origin.user.wallet
                    wallet.balance += refund_amt
                    wallet.save()
                    WalletTransaction.objects.create(
                        wallet=wallet,
                        amount=refund_amt,
                        balance=wallet.balance,
                        transaction_type='refund',
                        transaction=origin,
                        return_transaction=ret_trans
                    )

    def _restock_inventory(self, ret_trans, origin):
        # restock=True の場合、返品対象商品の在庫を元店舗に戻す
        for detail in ret_trans.return_details.all():
            stock_entry = Stock.objects.get(store_code=origin.store_code, jan=detail.jan)
            stock_entry.stock += detail.quantity
            stock_entry.save()

    def _copy_sale_products(self, origin, ret_trans):
        # 全返品の場合、元取引の全販売明細をそのままコピーする
        for detail in origin.sale_products.all():
            ReturnDetail.objects.create(
                return_transaction=ret_trans,
                jan=detail.jan,
                name=detail.name,
                price=detail.price,
                tax=detail.tax,
                discount=detail.discount,
                quantity=detail.quantity
            )

    def _process_partial_return(self, validated_data, ret_trans, origin):
        additional = validated_data.pop('additional_items', [])
        delete = validated_data.pop('delete_items', [])

        # 削除商品の明細を返品詳細として記録（値引きを考慮）
        for item in delete:
            jan = item.get('jan')
            qty = item.get('quantity')
            detail = next((d for d in origin.sale_products.all() if d.jan.jan == jan), None)
            if detail:
                ReturnDetail.objects.create(
                    return_transaction=ret_trans,
                    jan=detail.jan,
                    name=detail.name,
                    price=detail.price,
                    tax=detail.tax,
                    discount=detail.discount,
                    quantity=qty
                )

        # 修正会計（新規取引）の作成（承認番号不要）
        new_trans = self._create_new_transaction(origin, additional, delete, self.initial_data.get('payments', []))
        return new_trans

    def _create_new_transaction(self, origin, additional, delete, payments_input):
        """
        修正会計の作成処理
        - remaining_items: 元取引の販売明細から、削除分を差し引き、追加分を加えた明細リストを作成
        - store_code, terminal_id, staff_code はReturnTransactionSerializerの入力値から渡す
        - 新規取引総額 T_new を remaining_items から算出し、元取引総額 O との差 diff に応じて追加領収または返金を判断する
        - 修正会計の支払いは、支払い方法 "carryover" として登録（元の正の支払いはそのまま合算する）
        """
        remaining_items = []
        sale_dict = {d.jan.jan: d for d in origin.sale_products.all()}
        delete_counts = {item.get('jan'): int(item.get('quantity', 0)) for item in delete}

        # 元取引の販売明細から、削除分を差し引いた残りを組み立てる
        for jan, detail in sale_dict.items():
            remaining_qty = detail.quantity - delete_counts.get(jan, 0)
            if remaining_qty > 0:
                remaining_items.append({
                    'jan': jan,
                    'name': detail.name,
                    'price': detail.price,
                    'tax': int(detail.tax),
                    'discount': detail.discount,
                    'quantity': remaining_qty
                })

        # 追加商品の明細を加える
        for item in additional:
            jan = item.get('jan')
            qty = int(item.get('quantity'))
            product = Product.objects.filter(jan=jan).first()
            if not product:
                raise KeyError(f"JANコード {jan} の商品が存在しません。")
            remaining_items.append({
                'jan': jan,
                'name': product.name,
                'price': product.price,
                'tax': int(product.tax),
                'discount': item.get('discount', 0),
                'quantity': qty
            })

        # 追加商品と削除商品の合計金額（値引き考慮）の計算（共通化）
        add_total, del_total, _ = self._compute_additional_delete_totals(additional, delete, origin)

        # 引継支払の計算（処理方法は変更せず）
        if add_total - del_total > 0:
            # 追加領収発生：
            carryover_payment = {'payment_method': "carryover", 'amount': origin.total_amount}
            positive_payments = [p for p in payments_input if float(p['amount']) > 0]
            additional_payment = {
                'payment_method': positive_payments[0]['payment_method'] if positive_payments else "default_method",
                'amount': positive_payments[0]['amount'] if positive_payments else 0
            }
            combined_payments = [carryover_payment, additional_payment]
        elif add_total - del_total < 0:
            carry_over = origin.total_amount + add_total - del_total
            carryover_payment = {'payment_method': "carryover", 'amount': carry_over}
            combined_payments = [carryover_payment]
        else:
            carryover_payment = {'payment_method': "carryover", 'amount': origin.total_amount}
            combined_payments = [carryover_payment]

        new_data = {
            'original_transaction': origin.id,
            'sale_products': remaining_items,
            'store_code': self.initial_data.get('store_code'),
            'terminal_id': self.initial_data.get('terminal_id'),
            'staff_code': self.initial_data.get('staff_code'),
            'payments': combined_payments,
        }
        print("New Transaction Data:", new_data)
        serializer = TransactionSerializer(
            data=new_data, context={'external_data': True}
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()
