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


class ReturnDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReturnDetail
        fields = ['jan', 'name', 'price', 'tax', 'discount', 'quantity']


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
    # 元取引から引き継いだ商品の場合は True にする（通常は False）
    original_product = serializers.BooleanField(required=False, default=False)
    tax = serializers.IntegerField(required=False)
    discount = serializers.IntegerField(required=False, default=0)
    quantity = serializers.IntegerField(min_value=1)

    class Meta:
        model = TransactionDetail
        fields = ["jan", "name", "price", "tax", "discount", "quantity", "original_product"]
        # 通常はシステム側で設定されるので read_only とする
        read_only_fields = ["name", "price"]
        list_serializer_class = NonStopListSerializer

    def get_fields(self):
        fields = super().get_fields()
        # 返品（修正取引）の場合、元取引からの値を保持するため name, price を入力可能にする
        if self.context.get("return_context", False):
            fields["name"].read_only = False
            fields["price"].read_only = False
        return fields

    def validate(self, data):
        # --- 商品の存在確認 ---
        jan = data.get('jan')
        try:
            product = Product.objects.get(jan=jan)
        except Product.DoesNotExist:
            raise serializers.ValidationError({"jan": f"JANコード {jan} は登録されていません。"})
        
        # 元取引から引き継いだ商品の場合は、price, tax は必須とする
        if data.get("original_product", False):
            if data.get("price") is None:
                raise serializers.ValidationError({"price": "元取引から引き継いだ商品のpriceは必須です。"})
            if data.get("tax") is None:
                raise serializers.ValidationError({"tax": "元取引から引き継いだ商品のtaxは必須です。"})
            data["discount"] = data.get("discount") or 0
            return data

        # --- 税率のバリデーション ---
        # クライアント側から tax が指定されていなければ、商品の税率を使用
        specified_tax_rate = data.get('tax')
        try:
            applied_tax_rate = TaxRateManager.get_applied_tax(product, specified_tax_rate)
        except serializers.ValidationError as e:
            raise serializers.ValidationError({"tax": e.detail if hasattr(e, "detail") else str(e)})
        data['tax'] = applied_tax_rate
        data["discount"] = data.get("discount") or 0
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

        # 支払い金額のバリデーション（商品券は釣り銭が発行できないため、必要分のみ適用）
        if totals is not None:
            total_amount = totals['total_amount']
            payment_totals = {
                'cash': 0,
                'voucher': 0,
                'other': 0,    # 現金・金券以外（carryover は除外）
            }
            for payment in payments:
                method = payment.get('payment_method')
                amount = payment.get('amount', 0)
                if method == 'cash':
                    payment_totals['cash'] += amount
                elif method == 'voucher':
                    payment_totals['voucher'] += amount
                elif method != 'carryover':
                    payment_totals['other'] += amount

            provided_total = payment_totals['cash'] + payment_totals['voucher'] + payment_totals['other']
            if provided_total < total_amount:
                errors["payments"] = "支払いの合計金額が取引の合計金額を下回っています。"
            # 金券と現金の合計が合計金額を超えていないかのチェック
            cash_and_voucher_total = payment_totals['cash'] + payment_totals['voucher']
            if cash_and_voucher_total > total_amount and payment_totals['other'] > 0:
                errors["payments"] = "現金と金券を除く支払方法の総計が合計金額を超えています(金券充足)。"
            # 現金と金券を除く支払方法の総計が合計金額を超えているかのチェック
            if payment_totals['other'] > total_amount:
                errors["payments"] = "現金と金券を除く支払方法の総計が合計金額を超えています(キャッシュレス超過)。"

            # 支払い適用順序：金券 → その他 → 現金
            remaining = total_amount

            # 商品券（＝金券）は、必要な分のみ使用（超過分は change に含まれない）
            used_voucher = min(payment_totals['voucher'], remaining)
            remaining -= used_voucher

            # エラー: 金券が提供されているのに全く使用されていない場合
            if payment_totals['voucher'] > 0 and used_voucher == 0:
                errors["payments"] = "金券での支払いが行われていません。"

            used_other = min(payment_totals['other'], remaining)
            remaining -= used_other

            used_cash = min(payment_totals['cash'], remaining)
            remaining -= used_cash

            if remaining > 0:
                errors["payments"] = "支払いの合計金額が取引の合計金額を下回っています。"

            # トレーニング以外の場合はウォレット残高のチェック（withdraw は create 側で実施）
            if status != "training":
                user = data.get("user")
                wallet_payments = [p for p in payments if p['payment_method'] == 'wallet']
                if wallet_payments and user:
                    total_wallet_payment = sum(p['amount'] for p in wallet_payments)
                    if total_wallet_payment > user.wallet.balance:
                        shortage = total_wallet_payment - user.wallet.balance
                        errors["wallet"] = f"ウォレット残高不足。{int(shortage)}円分不足しています。"

        # -------------------------------
        if errors:
            raise serializers.ValidationError(errors)

        # 補助情報として計算結果を data に付与
        data["_totals"] = totals
        return data

    def _calculate_change(self, payments_data, total_amount):
        """
        釣り銭を計算する。金券は額面以上使用し、釣りは出ないルールを適用。
        """
        payment_totals = {
            'cash': 0,
            'voucher': 0,
            'other': 0,
        }
        
        for payment in payments_data:
            method = payment['payment_method']
            amount = payment['amount']
            
            if method == 'cash':
                payment_totals['cash'] += amount
            elif method == 'voucher':
                payment_totals['voucher'] += amount
            elif method != 'carryover':
                payment_totals['other'] += amount
        
        # キャッシュレス支払い（金券を除く）後の残額
        remaining_amount = total_amount - payment_totals['other']
        
        # 金券があれば、その金額を差し引く
        if payment_totals['voucher'] > 0:
            # 金券で支払う額（金券は額面以上使用で釣り銭なし）
            voucher_payment = min(payment_totals['voucher'], remaining_amount)
            remaining_amount -= voucher_payment
        
        # 現金での支払い
        cash_payment = min(payment_totals['cash'], remaining_amount)
        cash_change = payment_totals['cash'] - cash_payment
        
        return cash_change

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
            else:
                total_amount_tax8 += subtotal

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
            product = self._get_product(product_jan)
            if is_original:
                effective_price = data["detail_data"].get("price")
            else:
                store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
                effective_price = store_price.get_price() if store_price else product.price

            TransactionDetail.objects.create(
                transaction=transaction_instance,
                jan=product,
                name=product.name,
                price=effective_price,
                tax=tax_rate,
                discount=discount_value,
                quantity=aggregated_quantity
            )

    @transaction.atomic
    def create(self, validated_data):
        original_transaction = validated_data.pop("original_transaction", None)
        sale_products_data = validated_data.pop("sale_products", [])
        payments_data = validated_data.pop("payments", [])
        staff_code = validated_data.pop("staff_code", None)
        store_code = validated_data.pop("store_code", None)
        status = validated_data.get("status")
        terminal_id = validated_data.pop("terminal_id", None)
        totals = validated_data.pop("_totals", None)
        
        # 金券ルールに基づいた釣り銭計算
        change_amount = self._calculate_change(payments_data, totals["total_amount"])
        total_payments = sum(payment['amount'] for payment in payments_data)

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

        # 承認番号は内部処理用のため、コミット時に含めない
        validated_data.pop("approval_number", None)
        validated_data["date"] = timezone.now()
        validated_data.update({
            "status": status,
            "user": user,
            "terminal_id": terminal_id,
            "staff_code": staff_code,
            "store_code": store_code,
            "deposit": total_payments,
            "change": change_amount,  # 特殊な釣り銭計算結果を使用
            "total_quantity": totals["total_quantity"],
            "total_tax10": totals["total_tax10"],
            "total_tax8": totals["total_tax8"],
            "tax_amount": totals["tax_amount"],
            "discount_amount": totals["discount_amount"],
            "total_amount": totals["total_amount"]
        })
        transaction_instance = Transaction.objects.create(**validated_data)
        self._create_transaction_details(
            transaction_instance, sale_products_data, validated_data.get("status"), store_code
        )
        self._create_payments(transaction_instance, payments_data)

        # トレーニング以外の場合はウォレット減算処理
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
                # 現金の支払い合計を算出
                cash_payments_total = sum(float(p.get('amount', 0)) for p in payments if p.get('payment_method') == 'cash')
                if cash_payments_total > 0:
                    # 現金が含まれる場合は、expected_additional 以上であればOK
                    if payments_sum < expected_additional - 0.01:
                        errors['payments'] = f"追加領収金が不足しています。必要最低値: {expected_additional}, 入力値: {payments_sum}"
                else:
                    # 現金が含まれない場合は、厳密な一致を要求
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
        for detail in ret_trans.return_details.all():
            try:
                print(f"Restocking item: {detail.jan} for store: {origin.store_code}")
                stock_entry = Stock.objects.get(store_code=origin.store_code, jan=detail.jan)
                stock_entry.stock += detail.quantity
                stock_entry.save()
                print(f"Updated stock for {detail.jan}: {stock_entry.stock}")
            except Stock.DoesNotExist:
                print(f"Stock not found for JAN: {detail.jan} in store: {origin.store_code}")
                raise serializers.ValidationError(f"JANコード {detail.jan} の在庫が見つかりません。")

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
        
        # 関連付けの設定: 修正取引から返品取引への参照を設定
        print(new_trans)
        if new_trans:
            new_trans.relation_return_id = ret_trans
            origin.relation_return_id = ret_trans
            origin.save()
            new_trans.save()
            
        return new_trans

    def _create_new_transaction(self, original_transaction, additional_items, deleted_items, payment_inputs):
        """
        修正取引（resale）の新規作成処理です。
        元取引の販売明細から、削除数量を差し引いた残りと、追加商品の明細を統合し、正しい金額で新規取引を作成します。
        支払いは "carryover" を用いて元取引の正の支払いを引き継ぎます。
        """
        remaining_sale_products = []
        original_details = {detail.jan.jan: detail for detail in original_transaction.sale_products.all()}
        deleted_quantities = {item.get('jan'): int(item.get('quantity', 0)) for item in deleted_items}

        # 元取引の明細から削除分を引いた残りを作成
        for jan, sale_detail in original_details.items():
            remaining_qty = sale_detail.quantity - deleted_quantities.get(jan, 0)
            if remaining_qty > 0:
                remaining_sale_products.append({
                    'jan': jan,
                    'name': sale_detail.name,
                    'price': sale_detail.price,  # 元取引の価格を保持
                    'tax': int(sale_detail.tax),  # 元取引の税率を保持
                    'discount': sale_detail.discount,  # 元取引の割引を保持
                    'quantity': remaining_qty,
                    'original_product': True
                })

        # 追加商品の明細を追加
        for item in additional_items:
            jan = item.get('jan')
            qty = int(item.get('quantity'))
            product = Product.objects.filter(jan=jan).first()
            if not product:
                raise KeyError(f"JANコード {jan} の商品が存在しません。")
            remaining_sale_products.append({
                'jan': jan,
                'name': product.name,
                'price': product.price,
                'tax': int(product.tax),
                'discount': item.get('discount', 0),
                'quantity': qty,
                'original_product': False
            })

        additional_total, deleted_total, _ = self._compute_additional_delete_totals(additional_items, deleted_items, original_transaction)

        if additional_total - deleted_total > 0:
            carryover_payment = {'payment_method': "carryover", 'amount': original_transaction.total_amount}
            positive_payments = [p for p in payment_inputs if float(p['amount']) > 0]
            additional_payment = {
                'payment_method': positive_payments[0]['payment_method'] if positive_payments else "default_method",
                'amount': positive_payments[0]['amount'] if positive_payments else 0
            }
            combined_payments = [carryover_payment, additional_payment]
        elif additional_total - deleted_total < 0:
            carryover_amount = original_transaction.total_amount + additional_total - deleted_total
            carryover_payment = {'payment_method': "carryover", 'amount': carryover_amount}
            combined_payments = [carryover_payment]
        else:
            carryover_payment = {'payment_method': "carryover", 'amount': original_transaction.total_amount}
            combined_payments = [carryover_payment]

        new_transaction_data = {
            'original_transaction': original_transaction.id,
            'sale_products': remaining_sale_products,
            'store_code': self.initial_data.get('store_code'),
            'terminal_id': self.initial_data.get('terminal_id'),
            'staff_code': self.initial_data.get('staff_code'),
            'payments': combined_payments,
            'status': 'resale'
        }
        serializer = TransactionSerializer(
            data=new_transaction_data, context={'external_data': True, 'return_context': True}
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()


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