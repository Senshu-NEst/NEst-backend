from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from rest_framework.fields import empty
from .views import StockReceiveHistory, StockReceiveHistoryItem
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, Customer, WalletTransaction, Wallet, Approval, Store, ReturnTransaction, ReturnDetail, ReturnPayment, Department, POSA
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
        20桁で先頭が"999"の場合はPOSA販売と判断する。
        部門打ちの場合も税率や割引チェックは通常と同様に行う（ただしProduct存在チェックや在庫処理はスキップ）。
        POSA販売の場合は部門打ちとして処理し、追加でPOSA固有の検証を行う。
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

        # POSA販売処理（20桁、先頭999）
        if isinstance(jan, str) and len(jan) == 20 and jan.startswith("999"):
            return self._validate_posa_sale(data)

        # 部門打ち処理（8桁、先頭999）
        if isinstance(jan, str) and len(jan) == 8 and jan.startswith("999"):
            return self._validate_department_sale(data)

        # 通常商品処理
        return self._validate_normal_product(data)

    def _validate_posa_sale(self, data):
        """POSA販売の検証処理"""
        jan = data["jan"]
        price = data.get("price")
        quantity = data.get("quantity")
        discount = data.get("discount", 0) or 0

        # POSAコードの検証
        try:
            posa = POSA.objects.get(code=jan)
        except POSA.DoesNotExist:
            raise serializers.ValidationError({"jan": f"POSAコード {jan} は存在しません。"})

        # 販売数量チェック（1点のみ）
        if quantity != 1:
            raise serializers.ValidationError({"quantity": "POSAの販売数量は1点のみです。"})

        # 有効期限チェック（1ヶ月以上残っているか）
        from django.utils import timezone
        from datetime import timedelta
        today = timezone.localdate()
        one_month_later = today + timedelta(days=30)
        if posa.expiration_date < one_month_later:
            raise serializers.ValidationError({"jan": "POSAの有効期限が1ヶ月未満のため販売できません。"})

        # 識別子（先頭8桁）から部門情報を取得
        identifier = jan[:8]
        dept_code = identifier[3:]
        if len(dept_code) != 5:
            raise serializers.ValidationError({
                "jan": "POSAコードの識別子が不正です。"
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

        # 会計フラグチェック
        if dept.get_accounting_flag() != "allow":
            raise serializers.ValidationError({"jan": "指定された部門では販売が許可されていません。"})
        
        # POSAカードのステータスチェック
        if posa.status in ["BF_disabled", "AF_disabled"]:
            raise serializers.ValidationError({"jan": "事故POSAカードです。カードを回収の上、報告を行なってください。"})
        elif posa.status == "salled":
            raise serializers.ValidationError({"jan": "販売済みPOSAカードです。"})
        elif posa.status != "created":
            raise serializers.ValidationError({"jan": "無効なPOSAカードです。"})

        # 金額の処理
        if posa.is_variable:
            # バリアブルカードの場合は金額入力必須
            if price is None:
                raise serializers.ValidationError({"price": "バリアブルカードの場合、金額の入力が必須です。"})
            card_value = price
        else:
            # 固定額カードの場合
            if price is not None:
                raise serializers.ValidationError({"price": "固定額カードの場合、金額を指定することはできません。"})
            if posa.card_value is None:
                raise serializers.ValidationError({"jan": "POSAの金額が設定されていません。"})
            card_value = posa.card_value

        # 税率の処理（部門の税率を使用）
        dept_standard_tax = int(dept.get_tax_rate())
        input_tax = data.get("tax")

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

        # 値引きフラグチェック
        if discount > 0 and dept.get_discount_flag() != "allow":
            raise serializers.ValidationError({"discount": "この部門では割引が許可されていません。"})

        # POSAフラグを設定
        data["_is_posa"] = True
        data["_posa_instance"] = posa
        data["_is_department"] = True  # 部門打ちとしても処理する
        data["price"] = card_value
        data["tax"] = tax_to_apply
        data["discount"] = discount

        # 固有コード（12桁）を商品名に設定
        posa_unique_code = jan[8:]  # 後ろ12桁
        data["name"] = f'{dept_code}{dept.name}({posa_unique_code})'
        data["_department_name"] = dept.name
        data["jan"] = identifier  # JANは識別子（先頭8桁）に変更

        return data

    def _validate_department_sale(self, data):
        """部門打ち販売の検証処理（既存ロジックを分離）"""
        jan = data["jan"]
        price = data.get("price")
        quantity = data.get("quantity")
        discount = data.get("discount", 0) or 0

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

    def _validate_normal_product(self, data):
        """通常商品の検証処理（既存ロジックを分離）"""
        jan = data["jan"]
        price = data.get("price")
        discount = data.get("discount", 0) or 0

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

        # POSA販売時の支払い方法チェック（POSAのみの購入の場合は現金のみ）
        # ただし、再売取引（一部返品からの修正取引）の場合はPOSAチェックをスキップ
        posa_products = [p for p in sale_products if p.get("_is_posa", False)]
        non_posa_products = [p for p in sale_products if not p.get("_is_posa", False)]
        
        # 再売取引でない場合のみPOSA制限チェックを実行
        if not self.context.get("skip_posa_check", False):
            # POSAのみの販売の場合は現金支払いのみ許可
            if posa_products and not non_posa_products:
                non_cash_payments = [p for p in payments if p['payment_method'] != 'cash']
                if non_cash_payments:
                    errors["payments"] = "POSA単独販売時は現金での支払いのみ可能です。"

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
            
            # 再売取引でない場合のみPOSA混在チェックを実行
            if not self.context.get("skip_posa_check", False):
                # POSAと他商品混在時の支払い制限チェック
                if posa_products and non_posa_products:
                    posa_total = sum((p.get("price", 0) - p.get("discount", 0)) * p.get("quantity", 1) 
                                for p in posa_products)
                    non_posa_total = total_amount - posa_total
                    
                    # 現金支払い額を確認
                    cash_payment = sum(p['amount'] for p in payments if p['payment_method'] == 'cash')
                    if cash_payment < posa_total:
                        errors["payments"] = f"POSA商品分（{posa_total}円）は現金での支払いが必要です。現金不足: {posa_total - cash_payment}円"
            
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

            # 再売取引でない場合のみPOSAキャッシュレス制限チェックを実行
            if not self.context.get("skip_posa_check", False):
                # POSAがある場合のキャッシュレス決済上限チェック（POSAに現金充当を考慮）
                if posa_products:
                    posa_total = sum((p.get("price", 0) - p.get("discount", 0)) * p.get("quantity", 1) 
                                for p in posa_products)
                    
                    # POSAに現金を充当した後の残額を計算
                    cash_for_posa = min(payment_totals['cash'], posa_total)
                    remaining_after_posa_cash = total_amount - cash_for_posa
                    
                    # 残額に対する金券の有効額
                    effective_voucher = min(payment_totals['voucher'], remaining_after_posa_cash)
                    
                    # 金券使用後の残額に対するキャッシュレス決済上限チェック
                    remaining_after_voucher = remaining_after_posa_cash - effective_voucher
                    if payment_totals['other'] > remaining_after_voucher:
                        errors["payments"] = "キャッシュレス決済が必要額を超えています。"
                else:
                    # POSAがない場合の従来のロジック
                    effective_voucher = min(payment_totals['voucher'], total_amount)
                    
                    # キャッシュレス決済は、金券使用後の残り金額を超えての支払いは禁止
                    if payment_totals['other'] > (total_amount - effective_voucher):
                        errors["payments"] = "キャッシュレス決済が必要額を超えています。"
            else:
                # 再売取引の場合は従来のロジック（POSAチェックなし）
                effective_voucher = min(payment_totals['voucher'], total_amount)
                
                # キャッシュレス決済は、金券使用後の残り金額を超えての支払いは禁止
                if payment_totals['other'] > (total_amount - effective_voucher):
                    errors["payments"] = "キャッシュレス決済が必要額を超えています。"
            
            # 金券が登録されているのに利用されていない場合のチェック
            if payment_totals['voucher'] > 0 and effective_voucher == 0:
                errors["payments"] = "金券が登録されていますが、支払い金額が商品合計額を超過しているため利用できません。"

            # 現金は、過不足なく支払いに利用される。（過剰な現金はお釣りとして返却される）
            # 支払い全体の有効合計が不足している場合のみエラーとする
            # POSAがある場合とない場合で計算方法を分ける
            if posa_products and not self.context.get("skip_posa_check", False):
                # POSAがある場合：POSAに現金充当後の計算
                posa_total = sum((p.get("price", 0) - p.get("discount", 0)) * p.get("quantity", 1) 
                            for p in posa_products)
                cash_for_posa = min(payment_totals['cash'], posa_total)
                remaining_after_posa_cash = total_amount - cash_for_posa
                effective_voucher = min(payment_totals['voucher'], remaining_after_posa_cash)
                remaining_cash = payment_totals['cash'] - cash_for_posa
                
                total_effective_payment = cash_for_posa + effective_voucher + payment_totals['other'] + remaining_cash
            else:
                # POSAがない場合または再売取引の場合：従来の計算
                effective_voucher = min(payment_totals['voucher'], total_amount)
                total_effective_payment = effective_voucher + payment_totals['other'] + payment_totals['cash']
            
            if total_effective_payment < total_amount:
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

    def _calculate_change(self, payments_data, total_amount, sale_products_data):
        """
        釣り銭の計算：
        - POSAは現金でしか支払えない
        - 金券 (voucher), キャッシュレス (other) は釣り銭を発行しない（残額まで使用）
        - 現金 (cash) の過剰分のみ釣り銭として返却
        - 適用順序：現金でPOSA分を優先充当 → 残額に金券 → 残額にキャッシュレス → 残額に現金残り
        """
        # 1) POSA金額を算出
        posa_total = sum(
            item["price"] * item["quantity"]
            for item in sale_products_data
            if item.get("_is_department", False) and len(item.get("jan", "")) == 8
        )

        # 2) 支払額集計
        payment_totals = {'cash': 0, 'voucher': 0, 'other': 0}
        for p in payments_data:
            method = p['payment_method']
            amt = p['amount']
            if method == 'cash':
                payment_totals['cash'] += amt
            elif method == 'voucher':
                payment_totals['voucher'] += amt
            elif method != 'carryover':
                payment_totals['other'] += amt

        # 3) 支払い処理を順序立てて実行
        remaining_amount = total_amount
        used_cash = 0
        
        # ステップ1: POSA分を現金で優先充当
        if posa_total > 0:
            posa_cash_needed = min(payment_totals['cash'], posa_total)
            used_cash += posa_cash_needed
            remaining_amount -= posa_cash_needed
        
        # ステップ2: 残額に対して金券を適用（残額まで使用、釣り銭なし）
        voucher_to_use = min(payment_totals['voucher'], remaining_amount)
        remaining_amount -= voucher_to_use
        
        # ステップ3: 残額に対してキャッシュレスを適用（残額まで使用、釣り銭なし）
        other_to_use = min(payment_totals['other'], remaining_amount)
        remaining_amount -= other_to_use
        
        # ステップ4: 残額を現金の残りで支払い
        cash_remaining = payment_totals['cash'] - used_cash
        cash_for_remaining = min(cash_remaining, remaining_amount)
        used_cash += cash_for_remaining
        
        # 5) 釣り銭は「支払った現金」−「使った現金」
        change = payment_totals['cash'] - used_cash
        return max(0, change)

    def _aggregate_discount_errors(self, sale_products_data, store_code, permissions):
        discount_error_list = []
        for sale_product in sale_products_data:
            # 部門打ち・POSA販売の場合は割引チェックをスキップ
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
        POSA販売の場合は、部門打ちと同様に入力値を利用します。
        """
        total_quantity = 0
        total_amount_tax10 = 0
        total_amount_tax8 = 0
        total_amount_tax0 = 0  # 税率0の合計金額
        total_discount_amount = 0

        for sale_product in sale_products_data:
            # 部門打ち・POSA販売の場合は、製品情報に依存せず入力値を利用する
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

            # 税率に応じて合計金額を計算
            if applied_tax_rate == 10:
                total_amount_tax10 += subtotal
            elif applied_tax_rate == 8:
                total_amount_tax8 += subtotal
            elif applied_tax_rate == 0:  # 税率0の場合の処理
                total_amount_tax0 += subtotal
            else:
                pass

            total_quantity += quantity
            total_discount_amount += discount * quantity

        total_tax10 = int(total_amount_tax10 * 10 / 110)
        total_tax8 = int(total_amount_tax8 * 8 / 108)
        total_tax = total_tax10 + total_tax8
        total_amount = total_amount_tax10 + total_amount_tax8 + total_amount_tax0  # 税率0の金額も加算

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
        部門打ち・POSA販売の場合は_validate時に整形済みの値（_is_department, name, jan, price, tax等）を利用し、
        通常商品の場合は、Product存在チェックおよび在庫減算を行って TransactionDetail を作成します。
        POSA販売の場合は、販売後にPOSAのステータスを更新します。
        """
        approval_number_input = validated_data.pop("approval_number", None)
        original_transaction = validated_data.pop("original_transaction", None)
        sale_products_data = validated_data.pop("sale_products", [])
        payments_data = validated_data.pop("payments", [])
        staff_code = validated_data.pop("staff_code", None)
        store_code = validated_data.pop("store_code", None)
        status = validated_data.get("status")
        terminal_id = validated_data.pop("terminal_id", None)
        totals = validated_data.pop("_totals", None)

        # 釣り銭計算
        change_amount = self._calculate_change(payments_data, totals["total_amount"], sale_products_data)
        total_payments = sum(p["amount"] for p in payments_data)

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

        # トランザクション本体用データをセット
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
        # Transactionインスタンス作成
        transaction_instance = Transaction.objects.create(**validated_data)
        
        # 各明細の登録
        for sale_product in sale_products_data:
            # 部門打ち・POSA販売なら_ is_departmentフラグに従う
            if sale_product.get("_is_department", False):
                # 部門打ち・POSA販売の場合はvalidateで整形済みの値をそのまま利用（在庫減算等はスキップ）
                TransactionDetail.objects.create(
                    transaction=transaction_instance,
                    jan=sale_product["jan"],
                    name=sale_product.get("name") or sale_product.get("_department_name"),
                    price=sale_product["price"],
                    tax=sale_product["tax"],
                    discount=sale_product.get("discount", 0),
                    quantity=sale_product["quantity"]
                )
                
                # POSA販売の場合は、ステータスを更新
                if sale_product.get("_is_posa", False):
                    posa_instance = sale_product.get("_posa_instance")
                    if posa_instance and status != "training":
                        posa_instance.status = "salled"
                        posa_instance.buyer = user
                        posa_instance.relative_transaction = transaction_instance
                        posa_instance.save()
            else:
                # 通常商品の場合はProduct存在チェックおよび在庫減算を実施
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
        if status != "training" and user:
            wallet_payments = [p for p in payments_data if p["payment_method"] == "wallet"]
            if wallet_payments and user:
                total_wallet_payment = sum(p["amount"] for p in wallet_payments)
                user.wallet.withdraw(total_wallet_payment, transaction=transaction_instance)

        # 承認番号の使用済み更新（通常取引かつトレーニング以外の場合）
        if status == "sale" and approval_number_input:
            try:
                approval = Approval.objects.get(approval_number=approval_number_input)
            except Approval.DoesNotExist:
                raise serializers.ValidationError({"approval_number": "承認番号の処理時に内部エラーが発生しました。"})
            approval.is_used = True
            approval.save()

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
    additional_items = serializers.ListField(child=serializers.DictField(child=serializers.CharField(required=True)), required=False)
    delete_items = serializers.ListField(child=serializers.DictField(child=serializers.CharField(required=True)), required=False)

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

        # 元取引チェック
        origin_transaction = data.get('origin_transaction')
        if not origin_transaction:
            errors['origin_transaction'] = "元取引IDが必要です。"
        elif origin_transaction.status not in ['sale', 'resale']:
            errors['origin_transaction'] = "返品対象外取引です。"

        # スタッフ権限チェック
        try:
            self._check_permissions(
                data.get('staff_code'),
                store_code=data.get('store_code'),
                required_permissions=["register", "void"]
            )
        except serializers.ValidationError as e:
            errors['staff_code'] = e.detail

        # store_code 存在チェック
        store_code_val = data.get('store_code')
        try:
            data['store_code'] = Store.objects.get(store_code=store_code_val)
        except Store.DoesNotExist:
            errors['store_code'] = "指定された店舗コードは存在しません。"

        # return_type チェック
        allowed_types = dict(ReturnTransaction.RETURN_TYPE_CHOICES)
        allowed_types['payment_change'] = "支払い変更"
        return_type = data.get('return_type')
        if return_type not in allowed_types:
            errors['return_type'] = "不正なreturn_typeです。"

        # payments存在チェック
        payments = data.get('payments', [])
        if not payments:
            errors['payments'] = "支払い情報を入力してください。"

        # 部分返品時はアイテムを型変換・チェック
        if return_type == 'partial':
            data['additional_items'] = self._sanitize_items(
                data.get('additional_items', []), 'additional_items'
            )
            data['delete_items'] = self._sanitize_items(
                data.get('delete_items', []), 'delete_items'
            )

        # 各ケースの個別バリデーション
        if return_type == 'all':
            self._validate_all_return(payments, origin_transaction, errors)
        elif return_type == 'payment_change':
            self._validate_payment_change(data, payments, errors)
        elif return_type == 'partial':
            self._validate_partial(data, origin_transaction, payments, errors)

        if errors:
            raise serializers.ValidationError(errors)
        return data

    def _sanitize_items(self, items, group):
        """
        additional_items/delete_items の共通型変換と必須チェック。
        """
        sanitized = []
        for item in items:
            if 'jan' not in item or 'quantity' not in item:
                raise serializers.ValidationError({
                    group: f"{group}にはJANコードと数量が必須です。"
                })
            try:
                quantity = int(item['quantity'])
            except ValueError:
                raise serializers.ValidationError({
                    group: f"{group}の数量は整数でなければなりません。"
                })
            discount = None
            if 'discount' in item:
                try:
                    discount = float(item['discount'])
                except ValueError:
                    raise serializers.ValidationError({
                        group: f"{group}のdiscountは数値でなければなりません。"
                    })
            price = None
            if 'price' in item:
                try:
                    price = float(item['price'])
                except ValueError:
                    raise serializers.ValidationError({
                        group: f"{group}のpriceは数値でなければなりません。"
                    })
            obj = {'jan': item['jan'], 'quantity': quantity}
            if discount is not None:
                obj['discount'] = discount
            if price is not None:
                obj['price'] = price
            sanitized.append(obj)
        return sanitized

    def _validate_all_return(self, payments, origin_transaction, errors):
        negative_payments = [p for p in payments if float(p.get('amount', 0)) >= 0]
        if negative_payments:
            errors['payments'] = "全返品では返金金額をマイナスで入力する必要があります。"
        total = sum(abs(float(p['amount'])) for p in payments)
        if origin_transaction and abs(total - origin_transaction.total_amount) > 0.01:
            errors['payments'] = "返金額合計が元の合計金額と一致しません。"

    def _validate_payment_change(self, data, payments, errors):
        if data.get('additional_items') or data.get('delete_items'):
            errors['additional_items'] = "支払い変更の場合、追加・削除商品は入力できません。"
        positive_payments = [p for p in payments if float(p['amount']) > 0]
        if not positive_payments:
            errors['payments'] = "支払い変更では返金額と同額の支払い登録が必要です。"

    def _validate_partial(self, data, origin_transaction, payments, errors):
        if not data['additional_items'] and not data['delete_items']:
            errors['additional_items'] = "一部返品では追加・削除商品のいずれかが必須です。"
        try:
            additional_total, delete_total, net_amount = self._compute_additional_delete_totals(
                data['additional_items'], data['delete_items'], origin_transaction
            )
            payment_sum = sum(float(p['amount']) for p in payments)
            if net_amount > 0 and payment_sum < net_amount - 0.01:
                errors['payments'] = f"追加領収不足: 必要額{net_amount}, 入力額{payment_sum}"
            if net_amount < 0 and abs(payment_sum - net_amount) > 0.01:
                errors['payments'] = f"返金不足: 必要額{net_amount}, 入력額{payment_sum}"
        except serializers.ValidationError as e:
            detail = e.detail if isinstance(e.detail, dict) else {'validation': str(e)}
            errors.update(detail)

    def _compute_additional_delete_totals(self, additional_items, delete_items, origin_transaction):
        additional_total = sum(
            (float(Product.objects.filter(jan=item['jan']).first().price) - item.get('discount', 0))
            * item['quantity'] for item in additional_items
            if Product.objects.filter(jan=item['jan']).exists()
        )
        delete_total = 0
        original_products = list(origin_transaction.sale_products.all())
        products_list = [
            {
                'jan': p.jan,
                'name': p.name,
                'price': p.price,
                'tax': int(p.tax),
                'discount': p.discount,
                'quantity': p.quantity,
                'processed': False
            }
            for p in original_products
        ]
        for item in delete_items:
            jan = item['jan']
            delete_quantity = item['quantity']
            has_specific_tax = 'tax' in item and item['tax'] is not None
            has_specific_discount = 'discount' in item and item['discount'] is not None
            matching = [p for p in products_list if p['jan'] == jan and not p['processed']]
            if not matching:
                raise serializers.ValidationError({
                    'delete_items': f"JANコード {jan} の商品は元取引に存在しません。"
                })
            filtered = matching
            if has_specific_tax:
                specified_tax = int(item['tax'])
                filtered = [p for p in filtered if p['tax'] == specified_tax]
                if not filtered:
                    raise serializers.ValidationError({
                        'delete_items': f"JANコード {jan} で税率 {specified_tax} に一致する商品が見つかりません。"
                    })
            if has_specific_discount:
                specified_discount = float(item['discount'])
                filtered = [p for p in filtered if abs(p['discount'] - specified_discount) < 0.01]
                if not filtered:
                    raise serializers.ValidationError({
                        'delete_items': f"JANコード {jan} で値引き額 {specified_discount} に一致する商品が見つかりません。"
                    })
            if len(filtered) > 1:
                conditions = []
                if has_specific_tax:
                    conditions.append(f"税率 {item['tax']}")
                if has_specific_discount:
                    conditions.append(f"値引き額 {item['discount']}")
                raise serializers.ValidationError({
                    'delete_items': f"JANコード {jan} で条件「{', '.join(conditions)}」に一致する商品が複数あります。"
                })
            target = filtered[0]
            if delete_quantity > target['quantity']:
                raise serializers.ValidationError({
                    'delete_items': f"JANコード {jan} の削除数量 {delete_quantity} が元の数量 {target['quantity']} を超えています。"
                })
            delete_total += (target['price'] - target['discount']) * delete_quantity
            target['processed'] = (delete_quantity == target['quantity'])
            if delete_quantity < target['quantity']:
                target['quantity'] -= delete_quantity
        net_amount = additional_total - delete_total
        return additional_total, delete_total, net_amount

    def _process_payments(self, return_transaction, origin_transaction, payments_data):
        for payment in payments_data:
            amount = abs(float(payment.get('amount', 0)))
            ReturnPayment.objects.create(
                return_transaction=return_transaction,
                payment_method=payment.get('payment_method'),
                amount=amount
            )
            if payment.get('payment_method') == 'wallet' and float(payment.get('amount', 0)) < 0:
                wallet = origin_transaction.user.wallet
                wallet.balance += amount
                wallet.save()
                WalletTransaction.objects.create(
                    wallet=wallet,
                    amount=amount,
                    balance=wallet.balance,
                    transaction_type='refund',
                    transaction=origin_transaction,
                    return_transaction=return_transaction
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

    def _copy_sale_products(self, origin_transaction, return_transaction):
        for detail in origin_transaction.sale_products.all():
            ReturnDetail.objects.create(
                return_transaction=return_transaction,
                jan=detail.jan,
                name=detail.name,
                price=detail.price,
                tax=detail.tax,
                discount=detail.discount,
                quantity=detail.quantity
            )

    def _process_partial_return(self, validated_data, return_transaction, origin_transaction):
        for item in validated_data.get('delete_items', []):
            sale_detail = next((d for d in origin_transaction.sale_products.all() if d.jan == item['jan']), None)
            if sale_detail:
                ReturnDetail.objects.create(
                    return_transaction=return_transaction,
                    jan=sale_detail.jan,
                    name=sale_detail.name,
                    price=sale_detail.price,
                    tax=sale_detail.tax,
                    discount=sale_detail.discount,
                    quantity=item['quantity']
                )
        return None

    def _create_new_transaction_for_payment_change(self, original_transaction, return_transaction, new_payments):
        copied_products = []
        for detail in original_transaction.sale_products.all():
            copied_products.append({
                'jan': detail.jan,
                'name': detail.name,
                'price': detail.price,
                'tax': int(detail.tax),
                'discount': detail.discount,
                'quantity': detail.quantity,
                'original_product': True
            })
        data = {
            'original_transaction': original_transaction.id,
            'sale_products': copied_products,
            'store_code': self.initial_data.get('store_code'),
            'terminal_id': self.initial_data.get('terminal_id'),
            'staff_code': self.initial_data.get('staff_code'),
            'payments': new_payments,  # 正の金額のみ取り込み
            'status': 'resale'
        }
        serializer = TransactionSerializer(data=data, context={'external_data': True, 'return_context': True})
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def _create_new_transaction(
        self, original_transaction, additional_items, delete_items, payment_inputs
    ):
        additional_items = self._sanitize_items(additional_items, 'additional_items')
        delete_items = self._sanitize_items(delete_items, 'delete_items')

        original_products = list(original_transaction.sale_products.all())
        remaining_products = []
        products_list = [
            {
                'jan': p.jan,
                'name': p.name,
                'price': p.price,
                'tax': int(p.tax),
                'discount': p.discount,
                'quantity': p.quantity,
                'original_product': True,
                'processed': False
            }
            for p in original_products
        ]

        # 削除商品の処理
        for item in delete_items:
            jan = item['jan']
            delete_quantity = item['quantity']
            has_tax = 'tax' in item and item['tax'] is not None
            has_discount = 'discount' in item and item['discount'] is not None
            matching_products = [p for p in products_list if p['jan'] == jan and not p['processed']]
            if len(matching_products) > 1:
                if not (has_tax or has_discount):
                    raise serializers.ValidationError({
                        'delete_items': f"JANコード {jan} の商品が複数存在するため、税率または値引き額を指定して削除対象を特定してください。"
                    })
                filtered = matching_products
                if has_tax:
                    specified_tax = int(item['tax'])
                    filtered = [p for p in filtered if p['tax'] == specified_tax]
                if has_discount:
                    specified_discount = float(item['discount'])
                    filtered = [p for p in filtered if abs(p['discount'] - specified_discount) < 0.01]
                target = filtered[0] if filtered else matching_products[0]
            else:
                target = matching_products[0]
                if has_tax and target['tax'] != int(item['tax']):
                    raise serializers.ValidationError({
                        'delete_items': f"JANコード {jan} の指定税率 {item['tax']} が商品の税率 {target['tax']} と一致しません。"
                    })
                if has_discount and abs(target['discount'] - float(item['discount'])) > 0.01:
                    raise serializers.ValidationError({
                        'delete_items': f"JANコード {jan} の指定値引き額 {item['discount']} が商品の値引き額 {target['discount']} と一致しません。"
                    })
            if delete_quantity > target['quantity']:
                raise serializers.ValidationError({
                    'delete_items': f"JANコード {jan} の削除数量 {delete_quantity} が元の数量 {target['quantity']} を超えています。"
                })
            target['processed'] = True
            if delete_quantity < target['quantity']:
                remaining_products.append({
                    'jan': target['jan'],
                    'name': target['name'],
                    'price': target['price'],
                    'tax': target['tax'],
                    'discount': target['discount'],
                    'quantity': target['quantity'] - delete_quantity,
                    'original_product': True
                })

        # 未処理商品の追加
        for product in products_list:
            if not product['processed']:
                remaining_products.append({
                    'jan': product['jan'],
                    'name': product['name'],
                    'price': product['price'],
                    'tax': product['tax'],
                    'discount': product['discount'],
                    'quantity': product['quantity'],
                    'original_product': True
                })

        # 追加商品の処理
        for item in additional_items:
            jan = item['jan']
            quantity = item['quantity']
            discount = item.get('discount', 0)
            tax = item.get('tax')
            try:
                product_instance = Product.objects.get(jan=jan)
            except Product.DoesNotExist:
                raise serializers.ValidationError({
                    'additional_items': f"JANコード {jan} の商品が存在しません。"
                })
            if tax is None:
                tax = int(product_instance.tax)
            else:
                tax = int(tax)
            remaining_products.append({
                'jan': jan,
                'name': product_instance.name,
                'price': product_instance.price,
                'tax': tax,
                'discount': discount,
                'quantity': quantity,
                'original_product': False
            })

        new_transaction_data = {
            'original_transaction': original_transaction.id,
            'sale_products': remaining_products,
            'store_code': self.initial_data.get('store_code'),
            'terminal_id': self.initial_data.get('terminal_id'),
            'staff_code': self.initial_data.get('staff_code'),
            'payments': payment_inputs,
            'status': 'resale'
        }
        serializer = TransactionSerializer(data=new_transaction_data, context={'external_data': True, 'return_context': True})
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def _link_relation_return_ids(self, return_transaction, original_transaction, correction_transaction=None):
        original_transaction.relation_return_id = return_transaction
        original_transaction.save()
        if correction_transaction:
            correction_transaction.relation_return_id = return_transaction
            correction_transaction.save()
        return_transaction.save()

    @transaction.atomic
    def create(self, validated_data):
        return_type = validated_data.pop('return_type')
        original_transaction = validated_data['origin_transaction']
        #元取引のステータスを"返品"に変更
        original_transaction.status = 'return'
        original_transaction.save()

        # 返品先店舗を取得（存在しなければ ValidationError）
        store = validated_data.pop('store_code')
        # ReturnTransaction を作成
        return_transaction = ReturnTransaction.objects.create(
            origin_transaction=original_transaction,
            staff_code=validated_data.get('staff_code'),
            return_type=return_type,
            reason=validated_data.get('reason'),
            restock=validated_data.get('restock'),
            store_code=store,
            terminal_id=validated_data['terminal_id']
        )

        # 支払いを返金／新規支払いに分け、返金分だけ処理
        payments = validated_data.get('payments', [])
        if return_type == 'payment_change':
            refunds = [p for p in payments if float(p['amount']) < 0]
            new_payments = [p for p in payments if float(p['amount']) > 0]
            self._process_payments(return_transaction, original_transaction, refunds)
            correction_payments = new_payments
        else:
            self._process_payments(return_transaction, original_transaction, payments)
            correction_payments = None

        if return_type in ['all', 'payment_change']:
            self._copy_sale_products(original_transaction, return_transaction)
        elif return_type == 'partial':
            self._process_partial_return(
                validated_data, return_transaction, original_transaction
            )
        # 在庫戻し（payment_change ではスキップ
        if return_transaction.restock and return_type != 'payment_change':
            self._restock_inventory(return_transaction)

        # 修正取引（resale）の作成と modify_id への紐付け
        if return_type == 'payment_change':
            correction_payments = self._create_new_transaction_for_payment_change(
                original_transaction, return_transaction, correction_payments
            )
        elif return_type == 'partial':
            add_items = validated_data.get('additional_items', [])
            del_items = validated_data.get('delete_items', [])
            _, _, net_amount = self._compute_additional_delete_totals(
                add_items, del_items, original_transaction
            )
            refund_amount = abs(net_amount) if net_amount < 0 else 0
            carryover_amount = original_transaction.total_amount - refund_amount
            correction_payments = self._create_new_transaction(
                original_transaction,
                add_items,
                del_items,
                [{'payment_method': 'carryover', 'amount': carryover_amount}]
            )

        if correction_payments:
            return_transaction.modify_id = correction_payments
            return_transaction.save()

        # relation_return_id の紐付け（対象：元取引・修正取引）
        self._link_relation_return_ids(return_transaction, original_transaction, correction_payments)
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
