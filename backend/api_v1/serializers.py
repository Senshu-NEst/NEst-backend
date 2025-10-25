from rest_framework import serializers
from django.utils import timezone
from django.db import transaction
from rest_framework.fields import empty
from .views import StockReceiveHistory, StockReceiveHistoryItem
from .models import Product, Stock, Transaction, TransactionDetail, CustomUser, Customer, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, WalletTransaction, Wallet, Approval, Store, ReturnTransaction, ReturnDetail, ReturnPayment, Department, POSA, DiscountedJAN, Terminal
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
    # 各サブクラスでオーバーライドされるべき権限リスト
    required_permissions = []

    def validate_terminal(self, terminal_id, store_code=None):
        """
        store_codeの指定がない場合はterminal_idに紐づくStoreから取得
        最終的に使用すべきstore_codeとエラー情報を返す
        """
        if not terminal_id:
            return None, {"terminal_id": "端末IDが指定されていません。"}

        try:
            terminal = Terminal.objects.select_related('store').get(terminal_id=terminal_id)
        except Terminal.DoesNotExist:
            return None, {"terminal_id": f"端末ID '{terminal_id}' は登録されていません。"}

        if terminal.expires_at and terminal.expires_at < timezone.now():
            return None, {"terminal_id": f"端末ID '{terminal_id}' の有効期限が切れています。"}

        if not terminal.store:
            return None, {"terminal_id": f"端末ID '{terminal_id}' に店舗が紐付いていません。"}
        terminal_store_code = terminal.store.store_code

        # store_codeの指定がない場合はterminal_idに紐づくStoreから取得
        if not store_code:
            store_code = terminal_store_code

        # store_code が端末店舗と異なる場合の移動許可チェック
        if store_code != terminal_store_code and not terminal.allow_move:
            return store_code, {"terminal_id": f"端末ID '{terminal_id}' は店舗の移動が許可されていません。"}

        # 問題なし(エラーコードなし)
        return store_code, None

    def validate(self, data):
        """共通バリデーション：店舗の存在チェックとスタッフの権限チェック"""
        errors = {}
        staff_code = data.get("staff_code")
        store_code_val = data.get("store_code")

        # 1. 店舗の存在チェック
        try:
            store_instance = Store.objects.get(store_code=store_code_val)
            data['store_code'] = store_instance  # 後続処理のためにインスタンスを渡す
        except Store.DoesNotExist:
            errors['store_code'] = "不正な店舗コードです。"
            # 店舗が存在しない場合は以降のスタッフチェックができないため、ここで例外を発生
            raise serializers.ValidationError(errors)

        # 2. スタッフの権限チェック
        if not staff_code:
            errors["staff_code"] = "スタッフコードが指定されていません。"
        else:
            try:
                staff = Staff.objects.get(staff_code=staff_code)
                permission_checker = UserPermissionChecker(staff_code)
                permissions = permission_checker.get_permissions()

                # 所属店舗と処理対象店舗が異なる場合の権限チェック
                if staff.affiliate_store.store_code != store_instance.store_code:
                    if "global" not in permissions:
                        errors["staff_code"] = "このスタッフは自店のみ処理可能です。"
                
                # サブクラスで定義された権限のチェック
                if self.required_permissions:
                    missing = [p for p in self.required_permissions if p not in permissions]
                    if missing:
                        errors["staff_code"] = f"このスタッフは次の権限が不足しています: {', '.join(missing)}"
                
                # 後続処理のために権限情報を渡す
                data['_permissions'] = permissions

            except Staff.DoesNotExist:
                errors["staff_code"] = "指定されたスタッフが存在しません。"

        if errors:
            raise serializers.ValidationError(errors)
        
        return data


class TaxRateManager:
    @staticmethod
    def get_applied_tax(product, specified_tax_rate):
        # 指定がなければ、商品の元の税率を返す
        if specified_tax_rate is None:
            return product.tax
        # 指定税率は 0, 8, 10 のいずれかでなければならない
        if specified_tax_rate not in [0, 8, 10]:
            raise serializers.ValidationError(f"JAN:{product.jan} 不正な税率。税率は0, 8, 10のいずれかを指定してください。")
        # 税率10%の商品は変更不可
        if product.tax == 10 and specified_tax_rate != 10:
            raise serializers.ValidationError(f"JAN:{product.jan} 税率10%の商品は税率変更できません。")
        # 税率変更が禁止されている場合は、指定税率と元の税率が一致しなければならない
        if product.disable_change_tax:
            if specified_tax_rate != product.tax:
                raise serializers.ValidationError(f"JAN:{product.jan} は税率の変更が禁止されています。")
            return product.tax
        # 8%の商品について、指定が10%であれば変更を許可
        if product.tax == 8 and specified_tax_rate == 10:
            return 10
        # それ以外で、指定税率が元と異なる場合はエラー
        if specified_tax_rate != product.tax:
            raise serializers.ValidationError(f"JAN:{product.jan} は税率変更できません。")
        return product.tax


class NonStopListSerializer(serializers.ListSerializer):
    def run_validation(self, data=empty):
        if data is empty:
            return []
        ret = []
        errors = []
        has_errors = False
        
        for index, item in enumerate(data):
            try:
                value = self.child.run_validation(item)
                ret.append(value)
                errors.append(None)  # 正常なら None
            except serializers.ValidationError as exc:
                ret.append(item)
                errors.append(exc.detail)
                has_errors = True
        
        if has_errors:
            # 実際にエラーがあるインデックスのみを返す
            filtered_errors = {}
            for i, error in enumerate(errors):
                if error:
                    filtered_errors[i] = error
            raise serializers.ValidationError(filtered_errors)
        
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

        # 値引きJAN処理
        discounted_data = self._validate_discounted_jan(data)
        if discounted_data:
            return discounted_data

        # POSA販売処理（20桁、先頭999020）
        if isinstance(jan, str) and len(jan) == 20 and jan.startswith("999020"):
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
            raise serializers.ValidationError({"jan": "POSAコードの識別子が不正です。"})

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
            raise serializers.ValidationError({"jan": f"指定された部門コード {dept_code} に一致する部門が存在しません。"})

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
        data["_extra_code"] = posa_unique_code  # POSA固有番号をextra_codeに設定

        return data

    def _validate_department_sale(self, data):
        """部門打ち販売の検証処理（既存ロジックを分離）"""
        jan = data["jan"]
        price = data.get("price")
        discount = data.get("discount", 0) or 0

        data["_is_department"] = True

        # price必須チェック（部門打ち時）
        if price is None:
            raise serializers.ValidationError({"price": "部門打ちの場合、価格は必須項目です。"})

        # 部門コード分解
        dept_code = jan[3:]
        if len(dept_code) != 5:
            raise serializers.ValidationError({"jan": "部門打ちの場合、JANコードは '999' + 5桁の部門コードでなければなりません。"})
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

    def _validate_discounted_jan(self, data):
        """値引きJANの検証処理"""
        jan = data.get("jan")
        
        try:
            discounted_jan = DiscountedJAN.objects.select_related(
                'stock__jan', 
                'stock__store_code'
            ).get(instore_jan=jan)
        except DiscountedJAN.DoesNotExist:
            return None

        # ルートシリアライザからstore_codeを取得
        if not hasattr(self, 'root') or not self.root:
            return None
        store_code = self.root.initial_data.get('store_code')
        if not store_code:
            # store_codeが取得できない場合は、何もしない（TransactionSerializerのvalidateでエラーになるはず）
            return None

        # 店舗コードのチェック
        if discounted_jan.stock.store_code.store_code != store_code:
            raise serializers.ValidationError({"jan": "この店舗では使用できない値引きJANです。"})
        
        # 使用済みチェック
        if discounted_jan.is_used:
            raise serializers.ValidationError({"jan": "この値引きJANは既に使用済みです。"})

        original_product = discounted_jan.stock.jan
        
        # dataを値引きJANの情報で上書き
        data["jan"] = original_product.jan
        data["name"] = original_product.name
        data["price"] = discounted_jan.discounted_price
        data["tax"] = int(original_product.tax)
        data["discount"] = original_product.price - discounted_jan.discounted_price
        data["extra_code"] = discounted_jan.instore_jan
        data["_is_discounted"] = True
        data["_discounted_jan_instance"] = discounted_jan  # createメソッドで使うためにインスタンスを渡す

        return data


class TransactionSerializer(BaseTransactionSerializer):
    store_code = serializers.CharField(required=False)
    required_permissions = ["register"]
    original_transaction = serializers.PrimaryKeyRelatedField(queryset=Transaction.objects.all(), write_only=True, required=False)
    sale_products = TransactionDetailSerializer(many=True)
    payments = PaymentSerializer(many=True)
    date = serializers.DateTimeField(required=False)
    approval_number = serializers.CharField(required=False, write_only=True)

    class Meta:
        model = Transaction
        fields = ["id", "status", "date", "store_code", "terminal_id", "staff_code", "approval_number", "user", "total_tax10", "total_tax8", "tax_amount", "discount_amount", "total_amount", "deposit", "change", "total_quantity", "payments", "sale_products", "original_transaction"]
        read_only_fields = ["id", "user", "total_quantity", "total_tax10", "total_tax8", "tax_amount", "total_amount", "change", "discount_amount", "deposit"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.context.get("external_data", False):
            for field_name in self.Meta.read_only_fields:
                if field_name in self.fields:
                    self.fields[field_name].read_only = False

    def validate(self, data):
        errors = {}

        # ターミナルIDと店舗コードを入力値から取得
        terminal_id = data.get('terminal_id')
        store_code = data.get('store_code')

        # validate_terminalの1つ目の戻り値が決定された店舗となる（terminal_idからの店舗参照を含む）
        resolved_store_code, terminal_error = self.validate_terminal(terminal_id, store_code)
        if terminal_error:
            errors.update(terminal_error)
        data['store_code'] = resolved_store_code

        # BaseTransactionSerializerの共通バリデーションを呼び出す
        try:
            data = super().validate(data)
        except serializers.ValidationError as e:
            errors.update(e.detail)
        
        status = data.get("status")
        sale_products = data.get("sale_products")
        payments = data.get("payments")
        permissions = data.get("_permissions")

        # ステータスチェック
        allowed_statuses = ["resale"] if self.context.get("return_context") else ["sale", "training"]
        if status not in allowed_statuses:
            errors["status"] = "無効なstatusが指定されました。"

        # 商品・支払い情報の存在チェック
        if not sale_products:
            errors["sale_products"] = "少なくとも1つの商品を指定してください。"
        if not payments:
            errors["payments"] = "支払方法を指定してください。"
        if status != "resale" and payments and any(p['payment_method'] == 'carryover' for p in payments):
            errors["payments"] = "引継支払は再売以外で使用できません。"

        # 承認番号チェック
        if not self.context.get("external_data"):
            self._validate_approval_number(data.get("approval_number"), status, data, errors)

        # 商品関連のチェック
        if sale_products:
            store_code_instance = data.get("store_code")  # super().validate()でインスタンス化済み
            if store_code_instance:
                discount_errors = self._aggregate_discount_errors(sale_products, store_code_instance, permissions)
                if discount_errors:
                    errors["sale_products_discount"] = discount_errors
                
                try:
                    totals = self._calculate_totals(sale_products, store_code_instance)
                    data["_totals"] = totals
                    # 支払い関連のチェック
                    self._validate_all_payments(payments, totals, status, data.get("user"), errors)
                except serializers.ValidationError as e:
                    errors["sale_products_totals"] = e.detail

        if errors:
            raise serializers.ValidationError(errors)
        return data

    def _validate_approval_number(self, approval_number, status, data, errors):
        terminal_id = data.get("terminal_id")  # 会計リクエストのterminal_idを取得

        if not approval_number:
            errors["approval_number"] = "承認番号が入力されていません。"
        elif not (len(approval_number) == 8 and approval_number.isdigit()):
            errors["approval_number"] = "承認番号の形式に誤りがあります。数字8桁を入力してください。"
        else:
            try:
                approval = Approval.objects.select_related('terminal_id').get(approval_number=approval_number)
                if status != "training" and approval.is_used:
                    errors["approval_number"] = "この承認番号は使用済みです。"
                else:
                    # terminal_id の検証ロジックを追加
                    if approval.terminal_id and approval.terminal_id.terminal_id != terminal_id:
                        errors["approval_number"] = "承認番号が発行された端末と会計端末が一致しません。"
                    else:
                        data["user"] = approval.user
            except Approval.DoesNotExist:
                errors["approval_number"] = "承認番号が存在しません。"

    def _validate_all_payments(self, payments, totals, status, user, errors):
        total_amount = totals['total_amount']
        posa_products = [p for p in self.initial_data.get("sale_products", []) if p.get("_is_posa", False)]
        
        payment_totals = {'cash': 0, 'voucher': 0, 'other': 0}
        for p in payments:
            method = p.get('payment_method')
            amount = p.get('amount', 0)
            if method == 'cash':
                payment_totals['cash'] += amount
            elif method == 'voucher':
                payment_totals['voucher'] += amount
            else:
                payment_totals['other'] += amount

        payment_errors = self._validate_payment_amounts(payment_totals, total_amount, posa_products)
        if payment_errors:
            errors.update(payment_errors)

        if status != "training" and user:
            wallet_payments = [p for p in payments if p['payment_method'] == 'wallet']
            if wallet_payments:
                total_wallet_payment = sum(p['amount'] for p in wallet_payments)
                if total_wallet_payment > user.wallet.balance:
                    shortage = total_wallet_payment - user.wallet.balance
                    errors["wallet"] = f"ウォレット残高不足。{int(shortage)}円分不足しています。"

    def _validate_payment_amounts(self, payment_totals, total_amount, posa_products=None):
        """
        支払いバリデーションを効率的に実行
        優先順位：1. POSA現金充当 2. キャッシュレス決済上限チェック 3. 金券利用チェック
        すべてのエラーを収集してから一括で返す
        """
        errors = {}
        
        # 基本的な支払い総額チェック
        basic_payment_total = payment_totals['cash'] + payment_totals['voucher'] + payment_totals['other']
        if basic_payment_total < total_amount:
            errors["payment_total"] = "支払いの合計金額が不足しています。"
        
        # 支払い処理の優先順位に従った計算
        remaining_amount = total_amount
        
        # 1. POSA商品の現金充当（最優先）
        if posa_products and not self.context.get("skip_posa_check", False):
            posa_total = sum((p.get("price", 0) - p.get("discount", 0)) * p.get("quantity", 1) for p in posa_products)
            
            # POSA商品分の現金不足チェック
            if payment_totals['cash'] < posa_total:
                shortage = posa_total - payment_totals['cash']
                errors["posa_payment"] = f"POSA商品分（{posa_total}円）は現金での支払いが必要です。現金不足: {shortage}円"
            else:
                # POSA分を残額から差し引き
                remaining_amount -= posa_total
        
        # 2. キャッシュレス決済上限チェック（釣り銭が出せないため）
        if payment_totals['other'] > remaining_amount:
            errors["cashless_payment"] = "キャッシュレス決済が必要額を超えています。"
        else:
            # キャッシュレス決済分を残額から差し引き
            remaining_amount -= payment_totals['other']
        
        # 3. 金券利用チェック（キャッシュレス決済後の残額に対して）
        if payment_totals['voucher'] > 0:
            # 金券が実際に利用される額
            actual_voucher_usage = min(payment_totals['voucher'], remaining_amount)
            
            # 金券が登録されているのに全く利用されていない場合
            if actual_voucher_usage == 0:
                errors["voucher_payment"] = "金券が登録されていますが、利用されていません。"
            
            # 金券利用分を残額から差し引く
            remaining_amount -= actual_voucher_usage
        
        return errors

    def _calculate_change(self, payments_data, total_amount, sale_products_data):
        """
        釣り銭の計算：
        - POSAは現金でしか支払えない
        - 金券 (voucher), キャッシュレス (other) は釣り銭を発行しない（残額まで使用）
        - 現金 (cash) の過剰分のみ釣り銭として返却
        - 適用順序：現金でPOSA分を優先充当 → 残額にキャッシュレス → 残額に金券 → 残額に現金残り
        """
        # 1) POSA金額を算出
        posa_total = sum(
            item["price"] * item["quantity"]
            for item in sale_products_data
            if item.get("jan").startswith("99902000") and len(item.get("jan", "")) == 8
        )

        # 2) 支払額集計
        payment_totals = {'cash': 0, 'voucher': 0, 'other': 0}
        for payments in payments_data:
            method = payments['payment_method']
            amount = payments['amount']
            if method == 'cash':
                payment_totals['cash'] += amount
            elif method == 'voucher':
                payment_totals['voucher'] += amount
            elif method != 'carryover':
                payment_totals['other'] += amount

        # 3) 支払い処理を順序立てて実行
        remaining_amount = total_amount
        used_cash = 0

        # ステップ1: POSA分を現金で優先充当
        if posa_total > 0:
            posa_cash_needed = min(payment_totals['cash'], posa_total)
            used_cash += posa_cash_needed
            remaining_amount -= posa_cash_needed

        # ステップ2: 残額に対してキャッシュレスを適用（残額まで使用、合計金額超過不可）
        other_to_use = min(payment_totals['other'], remaining_amount)
        remaining_amount -= other_to_use
        
        # ステップ3: 残額に対して金券を適用（残額まで使用、合計金額超過分は釣り銭発行なし）
        voucher_to_use = min(payment_totals['voucher'], remaining_amount)
        remaining_amount -= voucher_to_use
        
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
            # 部門打ち・POSA販売・値引きJANの場合は割引チェックをスキップ
            if sale_product.get("_is_department", False) or sale_product.get("_is_discounted", False):
                continue
            jan = sale_product.get("jan", "不明")
            try:
                self._discount_check(sale_product, store_code, permissions)
            except serializers.ValidationError as e:
                discount_error_list.append({f"JAN:{jan}": e.detail})
        return discount_error_list

    def _discount_check(self, sale_product, store_code, permissions):
        errors = []
        jan = sale_product.get("jan", "不明")
        discount = sale_product.get("discount", 0)
        status = self.initial_data.get("status")
        
        # スタッフに売価変更権限がない場合のチェック（resale以外）
        if discount != 0 and status != "resale":
            if permissions is None or "change_price" not in permissions:
                errors.append(f"JAN:{jan} このスタッフは売価変更を行う権限がありません。")
        
        # 商品の安全な取得
        product, product_error = self._get_product(jan)
        if product_error:
            errors.append(f"JAN:{jan} {product_error}")
        else:
            # 割引禁止商品のチェック
            if discount > 0 and product.disable_change_price:
                errors.append(f"JAN:{jan} の値引きは許可されていません。")
            
            # 有効価格の取得と割引額のチェック
            store_price = StorePrice.objects.filter(store_code=store_code, jan=product).first()
            effective_price = store_price.get_price() if store_price else product.price
            
            if discount > effective_price:
                errors.append(f"JAN:{jan} 不正な割引額が入力されました。")
            
            if discount < 0:
                errors.append(f"JAN:{jan} 割引額は0以上である必要があります。")
        
        if errors:
            raise serializers.ValidationError(errors)

    def _calculate_totals(self, sale_products_data, store_code):
        """
        売上明細（sale_products）の合計金額、税額、割引額、数量を計算します。
        エラーが発生した場合は収集して一括で返す
        """
        errors = []
        total_quantity = 0
        total_amount_tax10 = 0
        total_amount_tax8 = 0
        total_amount_tax0 = 0
        total_discount_amount = 0

        for sale_product in sale_products_data:
            jan = sale_product.get("jan", "不明")
            
            try:
                # 部門打ち・POSA販売・値引きJANの場合は、製品情報に依存せず入力値を利用する
                if sale_product.get("_is_department", False) or sale_product.get("_is_discounted", False):
                    effective_price = sale_product.get("price")
                    applied_tax_rate = sale_product.get("tax")
                else:
                    # 商品の安全な取得
                    product, product_error = self._get_product(jan)
                    if product_error:
                        errors.append(f"JAN:{jan} - {product_error}")
                        continue  # このアイテムはスキップして次へ
                    
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

                total_quantity += quantity
                total_discount_amount += discount * quantity
                
            except Exception as e:
                errors.append(f"JAN:{jan} - 計算エラー: {str(e)}")

        if errors:
            raise serializers.ValidationError(errors)

        total_tax10 = int(total_amount_tax10 * 10 / 110)
        total_tax8 = int(total_amount_tax8 * 8 / 108)
        total_tax = total_tax10 + total_tax8
        total_amount = total_amount_tax10 + total_amount_tax8 + total_amount_tax0

        return {
            'total_quantity': total_quantity,
            'total_tax10': total_tax10,
            'total_tax8': total_tax8,
            'tax_amount': total_tax,
            'discount_amount': total_discount_amount,
            'total_amount': total_amount
        }

    def _get_product(self, jan_code):
        try:
            return Product.objects.get(jan=jan_code), None
        except Product.DoesNotExist:
            return None, f"JANコード {jan_code} は登録されていません。"
        except Exception as e:
            return None, f"商品取得エラー: {str(e)}"

    def _aggregate_same_products(self, sale_products_data):
        aggregated_products = {}
        for sale_product in sale_products_data:
            # 部門打ち商品の場合は価格も統合キーに含める
            if sale_product.get("_is_department", False):
                key = (
                    sale_product["jan"],
                    sale_product["tax"],
                    sale_product.get("discount", 0),
                    sale_product.get("original_product", False),
                    sale_product.get("price", 0)
                )
            else:
                key = (
                    sale_product["jan"],
                    sale_product["tax"],
                    sale_product.get("discount", 0),
                    sale_product.get("original_product", False)
                )
            if key not in aggregated_products:
                aggregated_products[key] = {
                    "data": sale_product.copy(),
                    "total_quantity": 0
                }
            aggregated_products[key]["total_quantity"] += sale_product["quantity"]

        return aggregated_products

    @transaction.atomic
    def create(self, validated_data):
        """
        部門打ち・POSA販売の場合は_validate時に整形済みの値（_is_department, name, jan, price, tax等）を利用し、
        通常商品の場合は、Product存在チェックおよび在庫減算を行って TransactionDetail を作成します。
        POSA販売の場合は、販売後にPOSAのステータスを更新します。
        """
        validated_data.pop("_permissions", None)
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
        
        # 商品統合処理：同一商品（jan, tax, discount, original_product）の数量を統合
        aggregated_products = self._aggregate_same_products(sale_products_data)

        # 統合された商品データで各明細を登録
        for key, aggregated_data in aggregated_products.items():
            sale_product = aggregated_data["data"]
            total_quantity = aggregated_data["total_quantity"]
            
            detail_data = {
                "transaction": transaction_instance,
                "quantity": total_quantity,
                "tax": sale_product["tax"],
                "discount": sale_product.get("discount", 0),
                "extra_code": sale_product.get("_extra_code", None)
            }

            # 部門打ち・POSA販売・値引きJANならフラグに従う
            if sale_product.get("_is_department", False) or sale_product.get("_is_discounted", False):
                detail_data.update({
                    "jan": sale_product["jan"],
                    "name": sale_product["name"],
                    "price": sale_product["price"],
                })
                # 値引きJANの場合は在庫を減算し、使用済みにする
                if sale_product.get("_is_discounted", False):
                    discounted_jan_instance = sale_product.get("_discounted_jan_instance")
                    if discounted_jan_instance:
                        stock = discounted_jan_instance.stock
                        if status != "training":
                            stock.stock -= total_quantity
                            stock.save()
                            discounted_jan_instance.is_used = True
                            discounted_jan_instance.save()
                    else:
                        # _validate_discounted_janで渡されるはずだが、念のため
                        raise serializers.ValidationError({"jan": f"値引きJAN {sale_product.get('extra_code')} の処理中にエラーが発生しました。"})
                
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
                            raise serializers.ValidationError({"sale_products": f"店舗コード {store_code} と JANコード {product.jan} の在庫は登録されていません。"})
                        stock.stock -= total_quantity
                        stock.save()
                
                detail_data.update({
                    "jan": product.jan,
                    "name": product.name,
                    "price": effective_price,
                })

            TransactionDetail.objects.create(**detail_data)

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
    required_permissions = ["register", "void"]
    return_type = serializers.CharField()
    reason = serializers.CharField()
    payments = ReturnPaymentSerializer(many=True, write_only=True)
    return_payments = ReturnPaymentSerializer(many=True, read_only=True)
    date = serializers.DateTimeField(source='created_at', read_only=True)
    store_code = serializers.CharField(required=False)
    terminal_id = serializers.CharField(required=True)
    return_products = ReturnDetailSerializer(many=True, source='return_details', read_only=True)
    additional_items = serializers.ListField(child=serializers.DictField(child=serializers.CharField(required=True)), required=False)
    delete_items = serializers.ListField(child=serializers.DictField(child=serializers.CharField(required=True)), required=False)

    class Meta:
        model = ReturnTransaction
        fields = ['id', 'origin_transaction', 'date', 'return_type', 'store_code', 'staff_code', 'terminal_id', 'reason', 'restock', 'payments', 'return_payments', 'return_products', 'additional_items', 'delete_items']
        extra_kwargs = {'origin_transaction': {'required': True}, 'staff_code': {'required': False}, 'restock': {'required': True}}

    def validate(self, data):
        errors = {}
        # ターミナルIDと店舗コードを入力値から取得
        terminal_id = data.get('terminal_id')
        store_code = data.get('store_code')

        # validate_terminalの1つ目の戻り値が決定された店舗となる（terminal_idからの店舗参照を含む）
        resolved_store_code, terminal_error = self.validate_terminal(terminal_id, store_code)
        if terminal_error:
            errors.update(terminal_error)
        data['store_code'] = resolved_store_code

        # BaseTransactionSerializerの共通バリデーションを呼び出す
        try:
            data = super().validate(data)
        except serializers.ValidationError as e:
            errors.update(e.detail)

        origin_transaction = data.get('origin_transaction')
        return_type = data.get('return_type')
        payments = data.get('payments', [])

        # 元取引のチェック
        if not origin_transaction:
            errors['origin_transaction'] = "不正な元取引IDです。"
        elif origin_transaction.status not in ['sale', 'resale']:
            errors['origin_transaction'] = "返品できない取引種別です。"

        # 返品種別のチェック
        if return_type not in dict(ReturnTransaction.RETURN_TYPE_CHOICES):
            errors['return_type'] = "不正な返品種別です。"

        # 支払い情報の存在チェック
        if not payments:
            errors['payments'] = "支払い情報を入力してください。"

        # 部分返品時のアイテムサニタイズ
        if return_type == 'partial':
            try:
                data['additional_items'] = self._sanitize_items(data.get('additional_items', []), 'additional_items')
                data['delete_items'] = self._sanitize_items(data.get('delete_items', []), 'delete_items')
            except serializers.ValidationError as e:
                errors.update(e.detail)

        # 各返品種別の個別バリデーション
        if not errors:  # 基本的なエラーがない場合のみ個別バリデーションに進む
            if return_type == 'all':
                self._validate_all_return(payments, origin_transaction, errors)
            elif return_type == 'payment_change':
                self._validate_payment_change(data, payments, errors)
            elif return_type == 'partial':
                self._validate_partial_return(data, origin_transaction, payments, errors)

        if errors:
            raise serializers.ValidationError(errors)
        return data

    def _sanitize_items(self, items, group):
        """
        additional_items/delete_items の共通型変換と必須チェック。
        POSA対応：999020から始まるjanコードを部門コード（前半8桁）+POSAコード（後半12桁）に分割
        """
        def _validate_integer(value, field_name, optional=False):
            if value is None:
                if optional:
                    return None
                raise serializers.ValidationError({group: f"{group}には{field_name}が必須です。"})

            try:
                return int(value)
            except ValueError:
                raise serializers.ValidationError({group: f"{group}の{field_name}は整数でなければなりません。"})

        sanitized = []
        for item in items:
            if 'jan' not in item or 'quantity' not in item:
                raise serializers.ValidationError({group: f"{group}にはJANコードと数量が必須です。"})

            # 数量、割引、価格の変換とチェック
            quantity = _validate_integer(item.get('quantity'), '数量')
            discount = _validate_integer(item.get('discount'), '値引額', optional=True)
            price = _validate_integer(item.get('price'), '価格', optional=True)

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
            errors['payments'] = "返金金額はマイナスで入力する必要があります。"
        total = sum(abs(float(p['amount'])) for p in payments)
        if origin_transaction and abs(total - origin_transaction.total_amount) > 0.01:
            errors['payments'] = "返金額合計が元の合計金額と一致しません。"
        # POSA存在チェック（全返品時）
        try:
            self._validate_posa_deletion(origin_transaction)
        except serializers.ValidationError as e:
            detail = e.detail if isinstance(e.detail, dict) else {'validation': str(e)}
            errors.update(detail)

    def _validate_partial_return(self, data, origin_transaction, payments, errors):
        if not data.get('additional_items') and not data.get('delete_items'):
            errors['additional_items'] = "一部返品では追加・削除商品のいずれかが必要です。"
            return

        # POSA削除可能チェック（一部返品時）
        delete_items = data.get('delete_items', [])
        if delete_items:
            try:
                self._validate_posa_deletion(origin_transaction, delete_items)
            except serializers.ValidationError as e:
                detail = e.detail if isinstance(e.detail, dict) else {'validation': str(e)}
                errors.update(detail)
                return
        # 追加・削除合計と支払い金額のバランスチェック（商品・POSA存在チェック含む）
        try:
            net_amount_tmp = (
                self._compute_additional_delete_totals(
                    data.get('additional_items', []),
                    data.get('delete_items', []),
                    origin_transaction
                )
            )
            net_amount = net_amount_tmp[-1]
        except serializers.ValidationError as e:
            detail = e.detail if isinstance(e.detail, dict) else {'validation': str(e)}
            errors.update(detail)
            return

        payment_sum = sum(int(p.get('amount', 0)) for p in payments)
        if net_amount > 0:  # 追加料金
            if payment_sum < net_amount - 0.01:
                errors['payments'] = f"追加支払いが不足しています。必要額: {net_amount}、入力額: {payment_sum}"
            elif payment_sum > net_amount + 0.01:
                errors['payments'] = f"追加支払いが多すぎます。必要額: {net_amount}、入力額: {payment_sum}"
        # 差額がマイナス（返金額が足りない）
        elif net_amount < 0:  # 追加支払いパターン
            expected_refund = -net_amount
            actual_refund = -payment_sum
            if (actual_refund - expected_refund) > 0:
                errors['payments'] = f"返金額が多すぎます。必要な返金額: {expected_refund}、入力額: {abs(actual_refund)}"
            elif (actual_refund - expected_refund) < 0:
                errors['payments'] = f"返金額が不足しています。必要な返金額: {expected_refund}、入力額: {abs(actual_refund)}"

    def _validate_payment_change(self, data, payments, errors):
        if data.get('additional_items') or data.get('delete_items'):
            errors['additional_items'] = "支払い変更の場合、追加・削除商品は入力できません。"
        positive_payments = [p for p in payments if float(p['amount']) > 0]
        if not positive_payments:
            errors['payments'] = "支払い変更では返金額と同額の支払い登録が必要です。"

    def _validate_posa_deletion(self, origin_transaction, delete_items_list=None):
        """
        全返品または一部返品時のPOSA削除可能性をチェック
        - origin_transaction: 元の取引
        - delete_items_list: 一部返品の場合の削除アイテムリスト（None の場合は全返品）
        """
        errors = {}
        # delete_items_listが空(全返品)の場合：元取引のすべての商品を走査
        if delete_items_list is None:
            posa_products = origin_transaction.sale_products.filter(
                jan__startswith='999020',
                extra_code__isnull=False  # 二重否定でわかりづらいがextra_codeが存在することをチェック
            ).exclude(extra_code='')

            for product in posa_products:
                posa_code = product.jan + product.extra_code
                try:
                    self._validate_single_posa(posa_code, origin_transaction, 'posa_validation')
                except serializers.ValidationError as e:
                    detail = e.detail if isinstance(e.detail, dict) else {'posa_validation': str(e)}
                    errors.update(detail)
        else:
            # delete_items_listに対してチェック
            jan_codes = [item['jan'] for item in delete_items_list if item['jan'].startswith('999020') and len(item['jan']) == 20]
            
            for item in delete_items_list:
                jan = item['jan']
                if jan in jan_codes:
                    posa_code = jan
                    # POSA商品の数量チェック（1以外はエラー）
                    delete_qty = item['quantity']
                    if delete_qty != 1:
                        errors['delete_items'] = f"POSAコード {posa_code} の数量は1である必要があります。指定された数量: {delete_qty}"
                        continue
                    
                    try:
                        self._validate_single_posa(posa_code, origin_transaction, 'delete_items')
                    except serializers.ValidationError as e:
                        detail = e.detail if isinstance(e.detail, dict) else {'delete_items': str(e)}
                        errors.update(detail)

        if errors:
            raise serializers.ValidationError(errors)

    def _validate_single_posa(self, posa_code, origin_transaction, field_name: str = 'posa_validation'):
        """
        単一のPOSAコードの存在とステータス検証
        field_nameはエラーを返す時のフィールド名
        """
        dept_code = posa_code[:8]
        extra_code = posa_code[8:]
        
        # 元取引に該当商品チェック
        if not origin_transaction.sale_products.filter(jan=dept_code, extra_code=extra_code).exists():
            raise serializers.ValidationError({field_name: f"元取引にPOSAコード {posa_code} を持つ商品が存在しません。"})

        # POSAモデル存在チェック
        try:
            posa = POSA.objects.get(code=posa_code)
        except POSA.DoesNotExist:
            raise serializers.ValidationError({field_name: f"POSAコード {posa_code} はPOSAマスターから削除されているため返品を続行できません。"})

        # ステータスチェック
        if posa.status != 'salled':
            raise serializers.ValidationError({field_name: f"POSAコード {posa_code} のステータスが'salled'ではないため削除できません。現在のステータス: {posa.status}"})

    def _disable_posa_status(self, posa_code):
        try:
            posa = POSA.objects.get(code=posa_code)
            posa.status = 'AF_disabled'
            posa.save()
        except POSA.DoesNotExist:
            raise serializers.ValidationError({'posa': f"POSAコード {posa_code} が見つかりません。"})

    def _process_posa_status_changes(self, origin_transaction, return_type, delete_items_list=None):
        """
        返品種別に応じてPOSAステータスを変更
        - return_transaction: 返品取引
        - origin_transaction: 元取引
        - return_type: 返品種別
        - delete_items_list: 一部返品の場合の削除アイテムリスト
        """
        if return_type == 'all':
            # 全返品の場合：元取引のすべてのPOSA商品を無効化
            posa_products = origin_transaction.sale_products.filter(jan__startswith='999020', extra_code__isnull=False).exclude(extra_code='')
            
            for product in posa_products:
                posa_code = product.jan + product.extra_code
                self._disable_posa_status(posa_code)
                
        elif return_type == 'partial' and delete_items_list:
            # 一部返品の場合：指定されたPOSA商品のみ無効化
            for item in delete_items_list:
                jan = item['jan']
                if jan.startswith('999020') and len(jan) == 20:
                    self._disable_posa_status(jan)

    def _compute_additional_delete_totals(self, additional_items, delete_items, origin_transaction):
        # リスト内包表記
        products_list = [
            {
                'jan': p.jan,
                'extra_code': p.extra_code,
                'price': p.price,
                'discount': p.discount,
                'tax': int(p.tax),
                'quantity': p.quantity,
                'processed': False
            }
            for p in origin_transaction.sale_products.all()
        ]
        # 追加商品の合計額計算 (元取引から価格を取得)
        additional_total = 0
        for item in additional_items:
            jan = item['jan']
            quantity = item['quantity']
            specified_discount = item.get('discount')

            # POSAの場合（999020から始まる20桁）
            if jan.startswith('999020') and len(jan) == 20:
                dept_code = jan[:8]
                code = jan[8:]
                
                # 元取引から該当商品の詳細を検索
                qs = origin_transaction.sale_products.filter(jan=dept_code, extra_code=code)
                detail = qs.first()
                if not detail:
                    raise serializers.ValidationError({'additional_items': f"POSAコード {jan} は元取引に存在しません。"})
            else:
                # 通常商品の場合
                qs = origin_transaction.sale_products.filter(jan=jan)
                detail = qs.first()
                if not detail:
                    raise serializers.ValidationError({'additional_items': f"元取引にJANコード {jan} の商品が存在しません。"})

            unit_price = detail.price
            discount = float(specified_discount) if specified_discount is not None else float(detail.discount)
            additional_total += (unit_price - discount) * quantity

        # 削除商品の合計額計算（POSAチェックは_validate_posa_deletionで実行済み）
        delete_total = 0
        for item in delete_items:
            jan = item['jan']
            delete_qty = item['quantity']
            specified_tax = item.get('tax')
            specified_discount = item.get('discount')

            # POSAの場合（999020から始まる20桁）- 存在チェックのみ
            if jan.startswith('999020') and len(jan) == 20:
                dept_code = jan[:8]
                code = jan[8:]
                
                # マッチング：部門コードとPOSAコード
                matching = [
                    p for p in products_list
                    if p['jan'] == dept_code and not p['processed'] and p['extra_code'] == code
                ]
                if not matching:
                    raise serializers.ValidationError({'delete_items': f"元取引に部門コード {dept_code}、POSAコード {code} の商品が存在しません。"})
            # 値引きJANの場合
            elif jan.startswith('20') and len(jan) == 13:
                matching = [
                    p for p in products_list
                    if p['extra_code'] == jan and not p['processed']
                ]
                if not matching:
                    raise serializers.ValidationError({'delete_items': f"元取引に値引きJAN {jan} の商品が存在しません。"})
            else:
                # 通常商品の場合
                matching = [
                    p for p in products_list
                    if p['jan'] == jan and not p['processed']
                ]
                if not matching:
                    raise serializers.ValidationError({'delete_items': f"元取引にJANコード {jan} の商品が存在しません。"})

            # 税率で絞り込み
            if specified_tax is not None:
                tax_int = int(specified_tax)
                matching = [p for p in matching if p['tax'] == tax_int]
                if not matching:
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} で税率 {specified_tax} に一致する商品が見つかりません。"})

            # 値引きで絞り込み
            if specified_discount is not None:
                disc_float = float(specified_discount)
                matching = [p for p in matching if abs(p['discount'] - disc_float) < 0.01]
                if not matching:
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} で値引き額 {specified_discount} に一致する商品が見つかりません。"})

            if len(matching) > 1:
                if jan.startswith('999020') and len(jan) == 20:
                    dept_code = jan[:8]
                    code = jan[8:]
                    raise serializers.ValidationError({'delete_items': f"部門コード {dept_code}、POSAコード {code} で条件に一致する商品が複数あります。税率や値引きを指定してください。"})
                else:
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} で条件に一致する商品が複数あります。税率や値引きを指定してください。"})

            target = matching[0]
            # 数量チェック（POSAの場合は_validate_posa_deletionで実行済み）
            if not (jan.startswith('999020') and len(jan) == 20):
                if delete_qty > target['quantity']:
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} の削除数量 {delete_qty} が元の数量 {target['quantity']} を超えています。"})

            delete_total += (target['price'] - target['discount']) * delete_qty
            if delete_qty == target['quantity']:
                target['processed'] = True
            else:
                target['quantity'] -= delete_qty

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
        返品先店舗に対して在庫を戻します。
        - 通常商品：StockReceiveHistory経由で在庫を戻す。
        - 値引きJAN商品：直接Stockを更新し、is_usedフラグをFalseに戻す。
        """
        return_details = return_transaction.return_details.all()
        normal_return_items = []
        
        for detail in return_details:
            # 値引きJAN（extra_codeに"20"で始まるJANがある）の場合
            if detail.extra_code and detail.extra_code.startswith('20'):
                try:
                    discounted_jan = DiscountedJAN.objects.get(instore_jan=detail.extra_code)
                    
                    # 在庫を戻す
                    stock = discounted_jan.stock
                    stock.stock += detail.quantity
                    stock.save()
                    
                    # 使用済みフラグを解除
                    discounted_jan.is_used = False
                    discounted_jan.save()
                    
                except DiscountedJAN.DoesNotExist:
                    # 念のためエラーハンドリング
                    continue
            # 通常商品（POSA等を除く）の場合
            elif not detail.jan.startswith('999'):
                normal_return_items.append(detail)

        # 通常商品の在庫戻し処理
        if not normal_return_items:
            return

        history = StockReceiveHistory.objects.create(
            store_code=return_transaction.store_code,
            staff_code=return_transaction.staff_code,
        )
        target_jan_codes = [detail.jan for detail in normal_return_items]
        products = {p.jan: p for p in Product.objects.filter(jan__in=target_jan_codes)}
        
        for detail in normal_return_items:
            product = products.get(detail.jan)
            if product:
                stock_entry, created = Stock.objects.get_or_create(
                    store_code=history.store_code,
                    jan=product,
                    defaults={'stock': detail.quantity}
                )
                if not created:
                    stock_entry.stock += detail.quantity
                    stock_entry.save()
                
                StockReceiveHistoryItem.objects.create(history=history, jan=product, additional_stock=detail.quantity)

    def _copy_sale_products(self, origin_transaction, return_transaction):
        for detail in origin_transaction.sale_products.all():
            # 返品明細を作成（extra_codeも含める）
            ReturnDetail.objects.create(
                return_transaction=return_transaction,
                jan=detail.jan,
                extra_code=detail.extra_code,
                name=detail.name,
                price=detail.price,
                tax=detail.tax,
                discount=detail.discount,
                quantity=detail.quantity
            )

    def _process_partial_return(self, validated_data, return_transaction, origin_transaction):
        for item in validated_data.get('delete_items', []):
            jan = item['jan']
            # POSAの場合（999020から始まる20桁）
            if jan.startswith('999020') and len(jan) == 20:
                dept_code = jan[:8]
                code = jan[8:]
                # 部門コードとPOSAコードでマッチング
                matching_details = origin_transaction.sale_products.filter(jan=dept_code, extra_code=code)
            else:
                # 通常商品の場合
                matching_details = origin_transaction.sale_products.filter(jan=jan)
            sale_detail = matching_details.first()
            if sale_detail:
                ReturnDetail.objects.create(
                    return_transaction=return_transaction,
                    jan=sale_detail.jan,
                    extra_code=sale_detail.extra_code,
                    name=sale_detail.name,
                    price=sale_detail.price,
                    tax=sale_detail.tax,
                    discount=sale_detail.discount,
                    quantity=item['quantity']
                )

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

    def _create_new_transaction(self, original_transaction, additional_items, delete_items, payment_inputs):
        additional_items = self._sanitize_items(additional_items, 'additional_items')
        delete_items = self._sanitize_items(delete_items, 'delete_items')
        original_products = list(original_transaction.sale_products.all())
        remaining_products = []
        products_list = [
            {
                'jan': p.jan,
                'extra_code': p.extra_code,
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
            
            # POSAの場合（999020から始まる20桁）
            if jan.startswith('999020') and len(jan) == 20:
                dept_code = jan[:8]
                code = jan[8:]
                
                # 部門コードとPOSAコードでマッチング
                matching_products = [p for p in products_list if p['jan'] == dept_code and p['extra_code'] == code and not p['processed']]
            else:
                # 通常商品の場合
                matching_products = [p for p in products_list if p['jan'] == jan and not p['processed']]
            
            if len(matching_products) > 1:
                if not (has_tax or has_discount):
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} の商品が複数存在するため、税率または値引き額を指定して削除対象を特定してください。"})
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
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} の指定税率 {item['tax']} が商品の税率 {target['tax']} と一致しません。"})
                if has_discount and abs(target['discount'] - float(item['discount'])) > 0.01:
                    raise serializers.ValidationError({'delete_items': f"JANコード {jan} の指定値引き額 {item['discount']} が商品の値引き額 {target['discount']} と一致しません。"})
            
            if delete_quantity > target['quantity']:
                raise serializers.ValidationError({'delete_items': f"JANコード {jan} の削除数量 {delete_quantity} が元の数量 {target['quantity']} を超えています。"})
            
            target['processed'] = True
            if delete_quantity < target['quantity']:
                remaining_products.append({
                    'jan': target['jan'],
                    'extra_code': target['extra_code'],
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
                    'extra_code': product['extra_code'],
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
            extra_code = item.get('extra_code')
            quantity = item['quantity']
            discount = item.get('discount', 0)
            tax = item.get('tax')
            
            # POSAの場合（999020から始まる20桁）
            if jan.startswith('999020') and len(jan) == 20:
                dept_code = jan[:8]
                code = jan[8:]
                
                # POSAの場合は部門マスタから情報を取得するか、デフォルト値を使用
                if tax is None:
                    tax = 10  # デフォルト税率（要調整）
                else:
                    tax = int(tax)
                
                remaining_products.append({
                    'jan': dept_code,  # 部門コードを使用
                    'extra_code': code,  # POSAコードを使用
                    'name': item.get('name', 'POSA商品'),  # デフォルト名
                    'price': item.get('price', 0),  # 価格は必須で渡されることを想定
                    'tax': tax,
                    'discount': discount,
                    'quantity': quantity,
                    'original_product': False
                })
            else:
                # 通常商品の処理
                try:
                    product_instance = Product.objects.get(jan=jan)
                except Product.DoesNotExist:
                    raise serializers.ValidationError({'additional_items': f"JANコード {jan} の商品が存在しません。"})
                
                if tax is None:
                    tax = int(product_instance.tax)
                else:
                    tax = int(tax)
                
                remaining_products.append({
                    'jan': jan,
                    'extra_code': extra_code,
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
        
        # 元取引のステータスを"返品"に変更
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
            self._process_partial_return(validated_data, return_transaction, original_transaction)
        
        # POSAステータス変更処理（全返品種別で確実に実行）
        delete_items = validated_data.get('delete_items', []) if return_type == 'partial' else None
        self._process_posa_status_changes(original_transaction, return_type, delete_items)
        
        # 在庫戻し（payment_change ではスキップ）
        if return_transaction.restock and return_type != 'payment_change':
            self._restock_inventory(return_transaction)

        # 修正取引（resale）の作成と modify_id への紐付け
        if return_type == 'payment_change':
            correction_payments = self._create_new_transaction_for_payment_change(original_transaction, return_transaction, correction_payments)
        elif return_type == 'partial':
            add_items = validated_data.get('additional_items', [])
            del_items = validated_data.get('delete_items', [])
            _, _, net_amount = self._compute_additional_delete_totals(add_items, del_items, original_transaction)
            refund_amount = abs(net_amount) if net_amount < 0 else 0
            carryover_amount = original_transaction.total_amount - refund_amount
            correction_payments = self._create_new_transaction(
                original_transaction, add_items, del_items,
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
        variation_details = ProductVariationDetail.objects.filter(product_variation=obj).select_related('product')
        return ProductVariationDetailSerializer(variation_details, many=True).data


class StockSerializer(serializers.ModelSerializer):
    standard_price = serializers.IntegerField(source='jan.price')
    store_price = serializers.SerializerMethodField()
    tax = serializers.IntegerField(source='jan.tax')
    name = serializers.CharField(source='jan.name', read_only=True)

    class Meta:
        model = Stock
        fields = ['store_code', 'name', 'jan', 'stock', 'standard_price', 'store_price', 'tax']
    # TODO: なぜmodels側のget_priceだけでは価格が取得できないのか調査する
    #  （現在のコードだとstore_priceが存在しない時のフォールバック処理を二回行なっていることになっている）

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


class StaffSerializer(serializers.ModelSerializer):
    """スタッフ情報用のシリアライザー"""
    store = serializers.SlugRelatedField(
        slug_field='store_code',
        read_only=True,
        source='affiliate_store'
    )
    name = serializers.CharField(read_only=True)
    role = serializers.SerializerMethodField()

    class Meta:
        model = Staff
        fields = ('staff_code', 'store', 'name', 'role')

    def get_role(self, obj):
        roles = []
        permission = obj.permission
        if permission:
            # UserPermissionモデルのBooleanFieldをリストアップ
            permission_fields = [
                'register_permission',
                'void_permission',
                'stock_receive_permission',
                'global_permission',
                'change_price_permission',
            ]
            for field_name in permission_fields:
                if getattr(permission, field_name):
                    roles.append(field_name)
        return roles


class CustomerSerializer(serializers.ModelSerializer):
    """顧客情報用のシリアライザー"""
    status = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = ('name', 'status')

    def get_status(self, obj):
        return (obj.user_status // 10) * 10
