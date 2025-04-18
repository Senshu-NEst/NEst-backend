from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from rest_framework.fields import empty
from .views import StockReceiveHistory, StockReceiveHistoryItem
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, Customer, WalletTransaction, Wallet, Approval, Store, ReturnTransaction, ReturnDetail, ReturnPayment, Department
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


class ReturnPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnPayment
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
    # JANは外部キーではなく文字列として受け取る
    jan = serializers.CharField()
    price = serializers.IntegerField(required=False)
    original_product = serializers.BooleanField(required=False, default=False)
    tax = serializers.IntegerField(required=False)
    discount = serializers.IntegerField(required=False, default=0)
    quantity = serializers.IntegerField(min_value=1)

    class Meta:
        model = TransactionDetail
        fields = ["jan", "name", "price", "tax", "discount", "quantity", "original_product"]
        # 通常はシステム側で設定されるので read_only とするが、
        # 返品（修正取引）の場合は入力可能にする。
        read_only_fields = ["name", "price"]
        list_serializer_class = NonStopListSerializer

    def get_fields(self):
        fields = super().get_fields()
        if self.context.get("return_context", False):
            fields["name"].read_only = False
            fields["price"].read_only = False
        return fields

    def validate(self, data):
        """
        入力されたJANが8桁で先頭が"999"の場合は部門打ちと判断し、
        部門打ちの場合も税率や割引チェックは通常と同様に行う（ただしProduct存在チェックや在庫処理はスキップ）。
        部門打ちの場合、validate時に部門（小分類）の名称を"name"および"_department_name"に設定し、
        JANは"999"+該当部門の連結コードに再設定する。
        """
        jan = data.get("jan")
        price = data.get("price")
        quantity = data.get("quantity")
        discount = data.get("discount", 0) or 0

        # 共通バリデーション: 数量と割引
        if quantity is None:
            raise serializers.ValidationError({"quantity": "数量は必須項目です。"})
        if price is not None and discount > price:
            raise serializers.ValidationError({"discount": "不正な割引額が入力されました。"})

        # 部門打ち処理
        if isinstance(jan, str) and len(jan) == 8 and jan.startswith("999"):
            data["_is_department"] = True

            # price必須チェック（部門打ち時）
            if price is None:
                raise serializers.ValidationError({"price": "部門打ちの場合、価格は必須項目です。"})

            # 部門コード分解
            dept_code = jan[3:]
            if len(dept_code) != 5:
                raise serializers.ValidationError({
                    "jan": "部門打ちの場合、JANコードは '999' + 5桁の部門コードでなければなりません。"
                })
            big_code = dept_code[0]
            middle_code = dept_code[1:3]
            small_code = dept_code[3:]

            try:
                dept = Department.objects.get(
                    level="small",
                    code=small_code,
                    parent__code=middle_code,
                    parent__parent__code=big_code,
                )
            except Department.DoesNotExist:
                raise serializers.ValidationError({
                    "jan": f"指定された部門コード {dept_code} に一致する部門が存在しません。"
                })

            # 会計フラグ
            if dept.get_accounting_flag() != "allow":
                raise serializers.ValidationError({"jan": "指定された部門では部門打ちが許可されていません。"})

            # dept マスタの税率取得
            dept_standard_tax = int(dept.get_tax_rate())
            input_tax = data.get("tax")

            # 入力税率があれば検証
            if input_tax in (None, ""):
                tax_to_apply = dept_standard_tax
            else:
                try:
                    input_tax = int(input_tax)
                except (ValueError, TypeError):
                    raise serializers.ValidationError({"tax": "税率は整数値で指定してください。"})
                tax_to_apply = input_tax

            # 税率変更フラグチェック
            if tax_to_apply != dept_standard_tax and dept.get_tax_rate_mod_flag() != "allow":
                raise serializers.ValidationError({"tax": "この部門では税率変更が許可されていません。"})

            data["tax"] = tax_to_apply

            # 値引きフラグ
            if discount > 0 and dept.get_discount_flag() != "allow":
                raise serializers.ValidationError({"discount": "この部門では割引が許可されていません。"})

            # 部門打ちの場合、商品名は部門の小分類の名称を設定する
            data["name"] = f'{dept_code}{dept.name}'
            data["_department_name"] = dept.name
            data["jan"] = "999" + dept.department_code

            return data

        # 通常商品処理
        try:
            product = Product.objects.get(jan=jan)
        except Product.DoesNotExist:
            raise serializers.ValidationError({"jan": f"JANコード {jan} は登録されていません。"})

        # 継承商品
        if data.get("original_product", False):
            if price is None:
                raise serializers.ValidationError({"price": "元取引から引き継いだ商品のpriceは必須です。"})
            if data.get("tax") is None:
                raise serializers.ValidationError({"tax": "元取引から引き継いだ商品のtaxは必須です。"})
            data["tax"] = int(data.get("tax"))
            data["discount"] = discount
            return data

        # 新規商品
        specified_tax = data.get("tax")
        try:
            applied_tax = TaxRateManager.get_applied_tax(product, specified_tax)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"tax": e.detail if hasattr(e, "detail") else str(e)})

        data["tax"] = applied_tax
        data["discount"] = discount
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
        read_only_fields = ["id", "user", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount", "deposit"]

    def __init__(self, *args, **kwargs):
        """
        コンテキストに 'external_data': True がある場合、read_only_fields も外部から上書きできるようにする。
        """
        super().__init__(*args, **kwargs)
        if self.context.get("external_data", False):
            for field_name in self.Meta.read_only_fields:
                if field_name in self.fields:
                    self.fields[field_name].read_only = False

    def validate(self, data):
        errors = {}

        # ステータスのチェック：ReturnTransactionの場合は "resale" のみ、通常は "sale" と "training" のみ許可する
        status = data.get("status")
        if self.context.get("return_context", False):
            allowed_statuses = ["resale"]
        else:
            allowed_statuses = ["sale", "training"]
        if status not in allowed_statuses:
            errors["status"] = "無効なstatusが指定されました。"

        sale_products = data.get("sale_products")
        if not sale_products or len(sale_products) == 0:
            errors["sale_products"] = "少なくとも1つの商品を指定してください。"

        payments = data.get("payments")
        if not payments or len(payments) == 0:
            errors["payments"] = "支払方法を指定してください。"

        if status != "resale" and any(item['payment_method'] == 'carryover' for item in payments):
            errors["payments"] = "引継支払は再売以外で使用できません。"

        # スタッフコードのチェックおよび権限チェック
        staff_code = data.get("staff_code")
        try:
            permissions = self._check_permissions(staff_code, data.get("store_code"), required_permissions=["register"])
        except serializers.ValidationError as e:
            errors["staff"] = e.detail
            permissions = None

        # 承認番号のチェック（external_data=Falseの場合）
        if not self.context.get("external_data", False):
            approval_number = data.get("approval_number")
            if not approval_number:
                errors["approval_number"] = "承認番号が入力されていません。"
            else:
                if not (len(approval_number) == 8 and approval_number.isdigit()):
                    errors["approval_number"] = "承認番号の形式に誤りがあります。数字8桁を入力してください。"
                else:
                    try:
                        approval = Approval.objects.get(approval_number=approval_number)
                        if status != "training" and approval.is_used:
                            errors["approval_number"] = "この承認番号は使用済みです。"
                        else:
                            data["user"] = approval.user
                    except Approval.DoesNotExist:
                        errors["approval_number"] = "承認番号が存在しません。"

        # 各商品の値引きチェック（各商品ごとにエラーを集約）
        discount_errors = self._aggregate_discount_errors(sale_products, data.get("store_code"), permissions)
        if discount_errors:
            errors["sale_products_discount"] = discount_errors

        # 支払い金額と商品合計金額のチェック
        try:
            totals = self._calculate_totals(sale_products, data.get("store_code"))
        except serializers.ValidationError as e:
            errors["sale_products_totals"] = e.detail
            totals = None

        if totals is not None:
            total_amount = totals['total_amount']
            # 各支払い方法（現金、金券、キャッシュレス）毎に合計額を算出
            payment_totals = {
                'cash': 0,
                'voucher': 0,  # 金券
                'other': 0,    # キャッシュレス（例: card, wallet, qr など）
            }
            for payment in payments:
                method = payment.get('payment_method')
                amount = payment.get('amount', 0)
                if method == 'cash':
                    payment_totals['cash'] += amount
                elif method == 'voucher':
                    payment_totals['voucher'] += amount
                else:
                    payment_totals['other'] += amount

            # 金券は合計金額まで有効。超過分は無視する。
            effective_voucher = min(payment_totals['voucher'], total_amount)

            # キャッシュレス決済は、金券使用後の残り金額を超えての支払いは禁止
            if payment_totals['other'] > (total_amount - effective_voucher):
                errors["payments"] = "キャッシュレス決済が必要額を超えています。"

            # 現金は、過不足なく支払いに利用される。（過剰な現金はお釣りとして返却される）
            # 支払い全体の有効合計が不足している場合のみエラーとする
            if (effective_voucher + payment_totals['other'] + payment_totals['cash']) < total_amount:
                errors["payments"] = "支払いの合計金額が不足しています。"

            # トレーニング以外の場合はウォレット残高のチェック（withdraw は create 側で実施）
            if status != "training":
                user = data.get("user")
                wallet_payments = [p for p in payments if p['payment_method'] == 'wallet']
                if wallet_payments and user:
                    total_wallet_payment = sum(p['amount'] for p in wallet_payments)
                    if total_wallet_payment > user.wallet.balance:
                        shortage = total_wallet_payment - user.wallet.balance
                        errors["wallet"] = f"ウォレット残高不足。{int(shortage)}円分不足しています。"

        if errors:
            raise serializers.ValidationError(errors)

        # 補助情報として計算結果を data に付与
        data["_totals"] = totals
        return data

    def _calculate_change(self, payments_data, total_amount):
        """
        釣り銭の計算：
        - 金券 (voucher) は必要額まで使用。超過分は無視（釣り銭にはならない）
        - キャッシュレス (other) も必要額内でのみ支払いを受け付け（釣り銭は発生しない）
        - 現金 (cash) は、残りの金額分に対して充当。過剰分が釣り銭として返却される
        """
        payment_totals = {'cash': 0, 'voucher': 0, 'other': 0}
        for payment in payments_data:
            method = payment['payment_method']
            amount = payment['amount']
            if method == 'cash':
                payment_totals['cash'] += amount
            elif method == 'voucher':
                payment_totals['voucher'] += amount
            elif method != 'carryover':
                payment_totals['other'] += amount

        effective_voucher = min(payment_totals['voucher'], total_amount)
        remaining = total_amount - effective_voucher

        used_other = min(payment_totals['other'], remaining)
        remaining -= used_other

        # 現金で支払うべき残り額
        required_cash = remaining
        used_cash = min(payment_totals['cash'], required_cash)
        change = payment_totals['cash'] - used_cash

        return change

    def _aggregate_discount_errors(self, sale_products_data, store_code, permissions):
        discount_error_list = []
        for sale_product in sale_products_data:
            # 部門打ちの場合は割引チェックをスキップ
            if sale_product.get("_is_department", False):
                continue
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
        """
        売上明細（sale_products）の合計金額、税額、割引額、数量を計算します。
        元取引から引き継いだ商品（original_product=True）の場合は、入力された price, tax, discount を使用し、
        新規追加商品の場合は店舗価格（または商品マスタの価格）と入力された tax（なければ商品マスタの税率）を使用します。
        """
        total_quantity = 0
        total_amount_tax10 = 0
        total_amount_tax8 = 0
        total_discount_amount = 0

        for sale_product in sale_products_data:
            # 部門打ちの場合は、製品情報に依存せず入力値を利用する
            if sale_product.get("_is_department", False):
                effective_price = sale_product.get("price")
                applied_tax_rate = sale_product.get("tax")
            else:
                product = self._get_product(sale_product["jan"])
                if sale_product.get("original_product", False):
                    effective_price = sale_product.get("price")
                    applied_tax_rate = sale_product.get("tax")
                else:
                    store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
                    effective_price = store_price.get_price() if store_price else product.price
                    applied_tax_rate = sale_product.get("tax", product.tax)
            discount = sale_product.get("discount") or 0
            quantity = sale_product.get("quantity") or 1
            subtotal = (effective_price - discount) * quantity

            if applied_tax_rate == 10:
                total_amount_tax10 += subtotal
            elif applied_tax_rate == 8:
                total_amount_tax8 += subtotal
            else:
                pass
            print(f'total tax 8:{total_amount_tax8}')

            total_quantity += quantity
            total_discount_amount += discount * quantity

        total_tax10 = int(total_amount_tax10 * 10 / 110)
        total_tax8 = int(total_amount_tax8 * 8 / 108)
        total_tax = total_tax10 + total_tax8
        total_amount = total_amount_tax10 + total_amount_tax8

        return {
            'total_quantity': total_quantity,
            'total_tax10': total_tax10,
            'total_tax8': total_tax8,
            'tax_amount': total_tax,
            'discount_amount': total_discount_amount,
            'total_amount': total_amount
        }

    def _process_product(self, store_code, sale_product_data, status):
        jan_value = sale_product_data.get("jan")
        if isinstance(jan_value, str) and jan_value.startswith("999"):
            # 部門打ちの場合は在庫減算をスキップし、price をそのまま返す
            return None, sale_product_data.get("price")
        else:
            product = self._get_product(jan_value)
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
        transaction_instance.payments.set(payment_instances)

    def _create_transaction_details(self, transaction_instance, sale_products_data, status, store_code):
        aggregated_details = {}
        for sale_product in sale_products_data:
            product_jan = sale_product.get("jan")
            tax_rate = sale_product.get("tax")
            discount_value = sale_product.get("discount", 0)
            quantity_value = sale_product.get("quantity", 1)
            is_original = sale_product.get("original_product", False)
            key = (product_jan, tax_rate, discount_value, is_original)
            if key not in aggregated_details:
                aggregated_details[key] = {"quantity": 0, "detail_data": sale_product}
            aggregated_details[key]["quantity"] += quantity_value

        for key, data in aggregated_details.items():
            product_jan, tax_rate, discount_value, is_original = key
            aggregated_quantity = data["quantity"]
            if data["detail_data"].get("_is_department", False):
                effective_price = data["detail_data"].get("price")
                # 商品名は必ず validate 時に設定された "name" または "_department_name" を利用
                name = data["detail_data"].get("name") or data["detail_data"].get("_department_name")
                # jan は文字列として格納しているので、そのまま利用
                TransactionDetail.objects.create(
                    transaction=transaction_instance,
                    jan=data["detail_data"].get("jan"),
                    name=name,
                    price=effective_price,
                    tax=tax_rate,
                    discount=discount_value,
                    quantity=aggregated_quantity
                )
            else:
                product = self._get_product(product_jan)
                if is_original:
                    effective_price = data["detail_data"].get("price")
                else:
                    store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
                    effective_price = store_price.get_price() if store_price else product.price
                TransactionDetail.objects.create(
                    transaction=transaction_instance,
                    jan=product.jan,
                    name=product.name,
                    price=effective_price,
                    tax=tax_rate,
                    discount=discount_value,
                    quantity=aggregated_quantity
                )

    def create(self, validated_data):
        """
        部門打ちの場合は_validate時に整形済みの値（_is_department, name, jan, price, tax等）を利用し、
        通常商品の場合は、Product存在チェックおよび在庫減算を行って TransactionDetail を作成します。
        """
        # Transaction用のデータ
        original_transaction = validated_data.pop("original_transaction", None)
        sale_products_data = validated_data.pop("sale_products", [])
        payments_data = validated_data.pop("payments", [])
        staff_code = validated_data.pop("staff_code", None)
        store_code = validated_data.pop("store_code", None)
        status = validated_data.get("status")
        terminal_id = validated_data.pop("terminal_id", None)
        totals = validated_data.pop("_totals", None)

        # 釣り銭計算などは従来通り
        change_amount = self._calculate_change(payments_data, totals["total_amount"])
        total_payments = sum(payment["amount"] for payment in payments_data)

        # original_transaction が指定されている場合、欠落項目を補完
        if original_transaction:
            # 修正会計の場合は、ユーザーを元取引から取得する
            user = original_transaction.user
            # external_data が True の場合は、ステータスを「再売」に設定
            if self.context.get("external_data", False):
                validated_data["status"] = "resale"
            else:
                validated_data.setdefault("status", original_transaction.status)
        else:
            user = validated_data.pop("user", [])

        validated_data.pop("approval_number", None)
        validated_data["date"] = timezone.now()
        validated_data.update({
            "status": status,
            "user": user,
            "terminal_id": terminal_id,
            "staff_code": staff_code,
            "store_code": store_code,
            "deposit": total_payments,
            "change": change_amount,
            "total_quantity": totals["total_quantity"],
            "total_tax10": totals["total_tax10"],
            "total_tax8": totals["total_tax8"],
            "tax_amount": totals["tax_amount"],
            "discount_amount": totals["discount_amount"],
            "total_amount": totals["total_amount"]
        })
        from .models import Transaction
        transaction_instance = Transaction.objects.create(**validated_data)
        
        # 各明細の登録
        for sale_product in sale_products_data:
            # 部門打ちなら_ is_departmentフラグに従う
            if sale_product.get("_is_department", False):
                # 部門打ちの場合はvalidateで整形済みの値をそのまま利用（在庫減算等はスキップ）
                TransactionDetail.objects.create(
                    transaction=transaction_instance,
                    jan=sale_product["jan"],
                    name=sale_product.get("name") or sale_product.get("_department_name"),
                    price=sale_product["price"],
                    tax=sale_product["tax"],
                    discount=sale_product.get("discount", 0),
                    quantity=sale_product["quantity"]
                )
            else:
                # 通常商品の場合はProduct存在チェックおよび在庫減算を実施
                from .models import Product, StorePrice
                try:
                    product = Product.objects.get(jan=sale_product["jan"])
                except Product.DoesNotExist:
                    raise serializers.ValidationError({"jan": f"JANコード {sale_product['jan']} は登録されていません。"})
                if sale_product.get("original_product", False):
                    effective_price = sale_product.get("price")
                else:
                    store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
                    effective_price = store_price.get_price() if store_price else product.price
                    if status != "training":
                        from .models import Stock
                        try:
                            stock = Stock.objects.get(store_code=store_code, jan=product)
                        except Stock.DoesNotExist:
                            raise serializers.ValidationError(
                                {"sale_products": f"店舗コード {store_code} と JANコード {product.jan} の在庫は登録されていません。"}
                            )
                        stock.stock -= sale_product.get("quantity", 1)
                        stock.save()
                TransactionDetail.objects.create(
                    transaction=transaction_instance,
                    jan=product.jan,
                    name=product.name,
                    price=effective_price,
                    tax=sale_product["tax"],
                    discount=sale_product.get("discount", 0),
                    quantity=sale_product["quantity"]
                )

        # 支払いの処理
        for payment_data in payments_data:
            Payment.objects.create(
                transaction=transaction_instance,
                payment_method=payment_data["payment_method"],
                amount=payment_data["amount"]
            )

        # ウォレット減算(トレーニング時は減算しない)
        if validated_data.get("status") != "training":
            wallet_payments = [p for p in payments_data if p["payment_method"] == "wallet"]
            if wallet_payments and user:
                total_wallet_payment = sum(p["amount"] for p in wallet_payments)
                user.wallet.withdraw(total_wallet_payment, transaction=transaction_instance)

        # 承認番号の使用済み更新（通常取引かつトレーニング以外の場合）
        if validated_data.get("status") == "sale":
            approval_number = validated_data.pop("approval_number", None)
            if approval_number:
                try:
                    approval = Approval.objects.get(approval_number=approval_number)
                    approval.is_used = True
                    approval.save()
                except Approval.DoesNotExist:
                    pass

        return transaction_instance


class ReturnDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnDetail
        fields = ['jan', 'name', 'price', 'tax', 'discount', 'quantity']


class ReturnTransactionSerializer(BaseTransactionSerializer):
    return_type = serializers.CharField()
    reason = serializers.CharField()
    payments = ReturnPaymentSerializer(many=True, write_only=True)
    return_payments = ReturnPaymentSerializer(many=True, read_only=True)
    date = serializers.DateTimeField(source='created_at', read_only=True)
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
        originTransaction = data.get('origin_transaction')
        if not originTransaction:
            errors['origin_transaction'] = "元取引IDが必要です。"
        elif originTransaction.status not in ['sale', 'resale']:
            errors['origin_transaction'] = "返品対象外取引です。"

        # スタッフ権限チェック
        staff_code = data.get("staff_code")
        try:
            self._check_permissions(staff_code, store_code=data.get('store_code'),
                                    required_permissions=["register", "void"])
        except serializers.ValidationError as e:
            errors["staff_code"] = e.detail

        # store_code の存在チェック
        store_code_val = data.get('store_code')
        try:
            store = Store.objects.get(store_code=store_code_val)
        except Store.DoesNotExist:
            errors['store_code'] = "指定された店舗コードは存在しません。"
        else:
            data['store_code'] = store

        # return_type のチェック
        allowed = dict(ReturnTransaction.RETURN_TYPE_CHOICES)
        allowed["payment_change"] = "支払い変更"
        returnType = data.get('return_type')
        if returnType not in allowed:
            errors['return_type'] = "不正なreturn_typeです。"

        payments = data.get('payments', [])
        if not payments:
            errors['payments'] = "支払い情報を入力してください。"

        # 各ケースのバリデーション
        if returnType == "all":
            # 全返品：すべて負数
            bad = [p for p in payments if float(p.get("amount", 0)) >= 0]
            if bad:
                errors['payments'] = "全返品では返金金額をマイナスで入力する必要があります。"
            total = sum(abs(float(p['amount'])) for p in payments)
            if originTransaction and abs(total - originTransaction.total_amount) > 0.01:
                errors['payments'] = "返金額合計がの合計金額と一致しません。"
        elif returnType == "payment_change":
            if data.get('additional_items') or data.get('delete_items'):
                errors['additional_items'] = "支払い変更の場合、追加・削除商品は入力できません。"
            new = [p for p in payments if float(p['amount']) > 0]
            if not new:
                errors['payments'] = "支払い変更では返金額と同額の支払い登録が必要です。"
        elif returnType == "partial":
            additionalItems = data.get('additional_items', [])
            deleteItems = data.get('delete_items', [])
            if not additionalItems and not deleteItems:
                errors['additional_items'] = "一部返品では追加・削除商品のいずれかが必須です。"
            self._validate_item_details(data)
            additionalTotal, deleteTotal, net = self._compute_additional_delete_totals(additionalItems, deleteItems, originTransaction)
            paymentsSum = sum(float(p['amount']) for p in payments)
            if net > 0 and paymentsSum < net - 0.01:
                errors['payments'] = f"追加領収不足: 必要額{net}, 入力額{paymentsSum}"
            if net < 0 and abs(paymentsSum - net) > 0.01:
                errors['payments'] = f"返金不足: 必要額{net}, 入力額{paymentsSum}"

        if errors:
            raise serializers.ValidationError(errors)
        return data

    def _validate_item_details(self, data):
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

    def _compute_additional_delete_totals(self, additional_items, delete_items, originTransaction):
        additionalTotal = sum(
            (float(Product.objects.filter(jan=item.get('jan')).first().price) - float(item.get('discount', 0)))
            * int(item.get('quantity', 1))
            for item in additional_items if Product.objects.filter(jan=item.get('jan')).exists()
        )
        deleteTotal = sum(
            (detail.price - detail.discount) * int(item.get('quantity', 1))
            for item in delete_items
            for detail in originTransaction.sale_products.all()
            if detail.jan == item.get('jan')
        )
        netReturnAmount = additionalTotal - deleteTotal
        return additionalTotal, deleteTotal, netReturnAmount

    def _process_payments(self, returnTransaction, originTransaction, paymentsData):
        for paymentItem in paymentsData:
            positiveAmount = abs(float(paymentItem.get("amount", 0)))
            ReturnPayment.objects.create(
                return_transaction=returnTransaction,
                payment_method=paymentItem.get("payment_method"),
                amount=positiveAmount
            )
            if paymentItem.get("payment_method") == "wallet" and float(paymentItem.get("amount", 0)) < 0:
                refundAmount = positiveAmount
                walletInstance = originTransaction.user.wallet
                walletInstance.balance += refundAmount
                walletInstance.save()
                WalletTransaction.objects.create(
                    wallet=walletInstance,
                    amount=refundAmount,
                    balance=walletInstance.balance,
                    transaction_type='refund',
                    transaction=originTransaction,
                    return_transaction=returnTransaction
                )

    def _restock_inventory(self, return_transaction):
        """
        返品先店舗(return_transaction.store_code)に対して、
        返品明細(return_transaction.return_details)の数量分を
        StockReceiveHistory/StockReceiveHistoryItem を経由して戻します。
        """
        # 1) 入荷履歴を作成（返品担当者＝入荷担当者）
        history = StockReceiveHistory.objects.create(
            store_code=return_transaction.store_code,
            staff_code=return_transaction.staff_code,
        )

        # 2) 各返品明細ごとに在庫を戻す
        for detail in return_transaction.return_details.all():
            jan_code = detail.jan  # char型のjanコードからproductインスタンスを取得
            try:
                product = detail.jan if isinstance(detail.jan, Product) else Product.objects.get(jan=jan_code)
            except Product.DoesNotExist:
                raise serializers.ValidationError({
                    "restock": f"JANコード {jan_code} の商品が存在しません。"
                })

            try:
                stock_entry = Stock.objects.get(store_code=history.store_code, jan=product)
            except Stock.DoesNotExist:
                raise serializers.ValidationError({
                    "restock": f"店舗 {history.store_code.store_code} に JANコード {product.jan} の在庫エントリが見つかりません。"
                })
            stock_entry.stock += detail.quantity
            stock_entry.save()

            StockReceiveHistoryItem.objects.create(
                history=history,
                jan=product,
                additional_stock=detail.quantity
            )

    def _copy_sale_products(self, originTransaction, returnTransaction):
        for detail in originTransaction.sale_products.all():
            ReturnDetail.objects.create(
                return_transaction=returnTransaction,
                jan=detail.jan,
                name=detail.name,
                price=detail.price,
                tax=detail.tax,
                discount=detail.discount,
                quantity=detail.quantity
            )

    def _process_partial_return(self, validatedData, returnTransaction, originTransaction):
        # 部分返品では、delete_itemsの内容をもとに返品明細を作成する（additional_itemsは後で修正取引用に使用）
        deleteItems = validatedData.get('delete_items', [])
        for item in deleteItems:
            itemJAN = item.get('jan')
            itemQuantity = item.get('quantity')
            saleDetail = next((d for d in originTransaction.sale_products.all() if d.jan == itemJAN), None)
            if saleDetail:
                ReturnDetail.objects.create(
                    return_transaction=returnTransaction,
                    jan=saleDetail.jan,
                    name=saleDetail.name,
                    price=saleDetail.price,
                    tax=saleDetail.tax,
                    discount=saleDetail.discount,
                    quantity=itemQuantity
                )
        # 本メソッドは返品明細処理のみを行い、修正取引の作成は後続で行う
        return None

    def _create_new_transaction_for_payment_change(self, originalTransaction, returnTransaction, newPaymentList):
        copiedSaleProducts = []
        for saleDetail in originalTransaction.sale_products.all():
            copiedSaleProducts.append({
                'jan': saleDetail.jan,
                'name': saleDetail.name,
                'price': saleDetail.price,
                'tax': int(saleDetail.tax),
                'discount': saleDetail.discount,
                'quantity': saleDetail.quantity,
                'original_product': True
            })
        newTransactionData = {
            'original_transaction': originalTransaction.id,
            'sale_products': copiedSaleProducts,
            'store_code': self.initial_data.get('store_code'),
            'terminal_id': self.initial_data.get('terminal_id'),
            'staff_code': self.initial_data.get('staff_code'),
            'payments': newPaymentList,  # 正の金額のみ
            'status': 'resale'
        }
        serializer = TransactionSerializer(
            data=newTransactionData, context={'external_data': True, 'return_context': True}
        )
        serializer.is_valid(raise_exception=True)
        newTransaction = serializer.save()
        return newTransaction

    def _create_new_transaction(self, originalTransaction, additionalItems, deletedItems, paymentInputs):
        remainingSaleProducts = []
        originalDetails = {detail.jan: detail for detail in originalTransaction.sale_products.all()}
        deletedQuantities = {item.get('jan'): int(item.get('quantity', 0)) for item in deletedItems}

        for janCode, saleDetail in originalDetails.items():
            remainingQuantity = saleDetail.quantity - deletedQuantities.get(janCode, 0)
            if remainingQuantity > 0:
                remainingSaleProducts.append({
                    'jan': janCode,
                    'name': saleDetail.name,
                    'price': saleDetail.price,
                    'tax': int(saleDetail.tax),
                    'discount': saleDetail.discount,
                    'quantity': remainingQuantity,
                    'original_product': True
                })

        for item in additionalItems:
            itemJAN = item.get('jan')
            itemQuantity = int(item.get('quantity'))
            productInstance = Product.objects.filter(jan=itemJAN).first()
            if not productInstance:
                raise KeyError(f"JANコード {itemJAN} の商品が存在しません。")
            remainingSaleProducts.append({
                'jan': itemJAN,
                'name': productInstance.name,
                'price': productInstance.price,
                'tax': int(productInstance.tax),
                'discount': item.get('discount', 0),
                'quantity': itemQuantity,
                'original_product': False
            })

        additionalTotal, deletedTotal, _ = self._compute_additional_delete_totals(additionalItems, deletedItems, originalTransaction)
        combinedPayments = paymentInputs

        newTransactionData = {
            'original_transaction': originalTransaction.id,
            'sale_products': remainingSaleProducts,
            'store_code': self.initial_data.get('store_code'),
            'terminal_id': self.initial_data.get('terminal_id'),
            'staff_code': self.initial_data.get('staff_code'),
            'payments': combinedPayments,
            'status': 'resale'
        }
        serializer = TransactionSerializer(
            data=newTransactionData, context={'external_data': True, 'return_context': True}
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def _link_relation_return_ids(self, returnTransaction, originalTransaction, correctionTransaction=None):
        originalTransaction.relation_return_id = returnTransaction
        originalTransaction.save()
        if correctionTransaction is not None:
            correctionTransaction.relation_return_id = returnTransaction
            correctionTransaction.save()
        returnTransaction.save()

    @transaction.atomic
    def create(self, validated_data):
        # 1. return_type を取り出し、元取引を取得
        return_type = validated_data.pop('return_type')
        original_transaction = validated_data['origin_transaction']

        # 2. 元取引を「返品」ステータスに更新
        original_transaction.status = 'return'
        original_transaction.save()

        # 3. 返品先店舗を取得（存在しなければ ValidationError）
        try:
            store_instance = Store.objects.get(store_code=validated_data.pop('store_code'))
        except Store.DoesNotExist:
            raise serializers.ValidationError({"store_code": "指定された店舗コードは存在しません。"})

        # 4. ReturnTransaction を作成
        return_transaction = ReturnTransaction.objects.create(
            origin_transaction=original_transaction,
            staff_code=validated_data.get('staff_code'),
            return_type=return_type,
            reason=validated_data.get('reason'),
            restock=validated_data.get('restock'),
            store_code=store_instance,
            terminal_id=validated_data['terminal_id']
        )

        # 5. 支払いを返金／新規支払いに分け、返金分だけ処理
        payments_input = validated_data.get('payments', [])
        if return_type == 'payment_change':
            refund_payments = [p for p in payments_input if float(p['amount']) < 0]
            new_payments = [p for p in payments_input if float(p['amount']) > 0]
            if not new_payments:
                raise serializers.ValidationError({"payments": "支払い変更の場合、新規支払い（正の金額）が少なくとも1件必要です。"})
            self._process_payments(return_transaction, original_transaction, refund_payments)
            correction_payments = new_payments
        else:
            # 全返品／一部返品とも、入力されたすべてを処理
            self._process_payments(return_transaction, original_transaction, payments_input)
            correction_payments = None

        # 6. 明細処理
        if return_type in ['payment_change', 'all']:
            # 在庫変動なしに全明細コピー
            self._copy_sale_products(original_transaction, return_transaction)
        elif return_type == 'partial':
            # delete_items に応じた返品明細
            self._process_partial_return(validated_data, return_transaction, original_transaction)

        # 7. restock=True の場合は返品先店舗に在庫を戻す（一部返品は delete_items のみ）
        if validated_data.get('restock', False):
            self._restock_inventory(return_transaction)

        # 8. 修正取引（resale）の作成と modify_id への紐付け
        correction_transaction = None
        if return_type == 'payment_change':
            correction_transaction = self._create_new_transaction_for_payment_change(
                original_transaction,
                return_transaction,
                correction_payments
            )
            return_transaction.modify_id = correction_transaction
            return_transaction.save()
        elif return_type == 'partial':
            additional_items = validated_data.get('additional_items', [])
            delete_items = validated_data.get('delete_items', [])
            _, _, net_amount = self._compute_additional_delete_totals(
                additional_items, delete_items, original_transaction
            )
            refund_amount = abs(net_amount) if net_amount < 0 else 0
            carryover_amount = original_transaction.deposit - refund_amount
            correction_payments = [{'payment_method': 'carryover', 'amount': carryover_amount}]
            correction_transaction = self._create_new_transaction(
                original_transaction,
                additional_items,
                delete_items,
                correction_payments
            )
            return_transaction.modify_id = correction_transaction
            return_transaction.save()

        # 9. relation_return_id の紐付け（元取引・修正取引 ← 返品取引）
        self._link_relation_return_ids(return_transaction, original_transaction, correction_transaction)

        return return_transaction


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
