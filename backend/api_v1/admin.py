from django.contrib import admin, messages
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from django import forms
from .models import Product, Store, Stock, Transaction, TransactionDetail, CustomUser, UserPermission, StockReceiveHistory, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail, Staff, Customer, Wallet, WalletTransaction, Approval, ReturnTransaction, ReturnDetail, ReturnPayment, Department, POSA, BulkGeneratePOSACodes, DiscountedJAN, DailySalesReport
from django.utils import timezone
from . import utils
from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.apps import apps
from django.template.response import TemplateResponse
from rest_framework_simplejwt.token_blacklist.admin import BlacklistedTokenAdmin as DefaultBlacklistedTokenAdmin, OutstandingTokenAdmin as DefaultOutstandingTokenAdmin
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from django.urls import reverse
from django.utils.html import format_html
from .rules import check_transaction_access, filter_transactions_by_user


class NegativeStockFilter(admin.SimpleListFilter):
    title = '在庫数'
    parameter_name = 'negative_stock'

    def lookups(self, request, model_admin):
        return (('negative', 'マイナス在庫'),)

    def queryset(self, request, queryset):
        return queryset.filter(stock__lt=0) if self.value() == 'negative' else queryset


class ProductResource(resources.ModelResource):
    class Meta:
        model = Product
        import_id_fields = ('jan',)
        skip_unchanged = True


class StoreResource(resources.ModelResource):
    class Meta:
        model = Store
        import_id_fields = ('store_code',)
        skip_unchanged = True


class TransactionDetailInline(admin.TabularInline):
    model = TransactionDetail
    extra = 0
    verbose_name = "取引詳細"
    verbose_name_plural = "取引詳細"


class PaymentDetailInline(admin.TabularInline):
    model = Payment
    extra = 0
    verbose_name = "支払"
    verbose_name_plural = "支払詳細"


class StockInline(admin.TabularInline):
    model = Stock
    extra = 0
    verbose_name = "在庫"
    verbose_name_plural = "在庫情報"


class VariationDetailInline(admin.TabularInline):
    model = ProductVariationDetail
    extra = 1
    verbose_name = "商品色名"
    verbose_name_plural = "商品色名一覧"


@admin.register(Product)
class ProductAdmin(ImportExportModelAdmin):
    resource_class = ProductResource
    list_display = ("jan", "department_code__name", "name", "price", "tax", "status")
    fields = ("jan","department_code", "name", ("price", "tax"), "status", ("disable_change_tax", "disable_change_price"))
    search_fields = ("name", "jan")
    list_filter = ("status", "tax")
    inlines = [StockInline]
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "department_code":
            kwargs["queryset"] = Department.objects.filter(level='small')  # 小分類のみ
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(ProductVariation)
class ProductVariationAdmin(admin.ModelAdmin):
    list_display = ("instore_jan", "name")
    search_fields = ("instore_jan", "name")
    inlines = [VariationDetailInline]


@admin.register(DiscountedJAN)
class DiscountedJANAdmin(admin.ModelAdmin):
    list_display = ('instore_jan', 'get_store_name', 'get_product_jan', 'get_product_name', 'get_original_price', 'discounted_price', 'get_current_stock', 'is_used')
    search_fields = ('instore_jan', 'stock__jan__jan', 'stock__jan__name', 'stock__store_code__name')
    list_filter = ('stock__store_code__name', 'is_used')
    raw_id_fields = ('stock',)
    readonly_fields = ('get_stock_info',)
    fields = ('stock', 'discounted_price', 'get_stock_info')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('stock__store_code', 'stock__jan')

    def get_store_name(self, obj):
        return obj.stock.store_code.name
    get_store_name.short_description = '店舗名'
    get_store_name.admin_order_field = 'stock__store_code__name'

    def get_product_jan(self, obj):
        return obj.stock.jan.jan
    get_product_jan.short_description = '元JAN'
    get_product_jan.admin_order_field = 'stock__jan__jan'

    def get_product_name(self, obj):
        return obj.stock.jan.name
    get_product_name.short_description = '商品名'
    get_product_name.admin_order_field = 'stock__jan__name'

    def get_original_price(self, obj):
        return obj.stock.jan.price
    get_original_price.short_description = '元価格'

    def get_current_stock(self, obj):
        return obj.stock.stock
    get_current_stock.short_description = '現在庫数'
    
    def get_stock_info(self, obj):
        if obj.pk:  # オブジェクトが保存されている場合のみ
            return format_html(
                "<b>店舗:</b> {}<br><b>商品:</b> {} ({})<br><b>現在庫:</b> {}",
                obj.stock.store_code.name,
                obj.stock.jan.name,
                obj.stock.jan.jan,
                obj.stock.stock
            )
        return "在庫を選択して保存すると、情報が表示されます。"
    get_stock_info.short_description = '在庫情報'


@admin.register(StorePrice)
class StorePriceAdmin(admin.ModelAdmin):
    list_display = ("store_code", "jan", "jan__name", "price")
    search_fields = ("store_code", "jan")
    list_filter = ("store_code",)


@admin.register(Store)
class StoreAdmin(ImportExportModelAdmin):
    resource_class = StoreResource
    list_display = ("store_code", "name")
    search_fields = ("name", "store_code")

    def get_readonly_fields(self, request, obj=None):
        return [] if obj is None else ("store_code",)


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    readonly_fields = ("updated_at",)
    list_display = ("store_code", "jan", "jan__name", "stock")
    search_fields = ("jan__name", "jan__jan")
    list_filter = ("store_code", NegativeStockFilter)


class StockReceiveHistoryItemInline(admin.TabularInline):
    model = StockReceiveHistoryItem
    extra = 0
    verbose_name = "入荷"
    verbose_name_plural = "入荷商品"


@admin.register(StockReceiveHistory)
class StockReceiveHistoryAdmin(admin.ModelAdmin):
    list_display = ("received_at", "store_code__store_code", "staff_code__name")
    list_filter = ("received_at", "store_code", "staff_code__name")
    inlines = [StockReceiveHistoryItemInline]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ユーザーのアクセス権限に基づいてクエリセットをフィルタリング
        return filter_transactions_by_user(request.user, qs)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "store_code", "staff_code", "status", "total_amount", "receipt_button")
    fieldsets = [
        ('取引情報', {'fields': ("status", "relation_return_id", "date", ("store_code", "staff_code", "user", "terminal_id"))}),
        ('消費税', {'fields': (("total_tax10", "total_tax8"), "tax_amount")}),
        ('金額情報', {'fields': ("discount_amount", ("deposit", "change"), ("total_quantity", "total_amount"))}),
    ]
    search_fields = ("id", "store_code__store_code", "staff_code__staff_code")
    list_filter = ("status", "date", "staff_code__name", "store_code")
    ordering = ("-id",)
    inlines = [PaymentDetailInline, TransactionDetailInline]

    def receipt_button(self, obj):
        return format_html('<a class="button" href="{}">レシート</a>', reverse('generate_receipt_view', args=[obj.id, 'sale']))
    receipt_button.short_description = 'レシートを表示'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ユーザーのアクセス権限に基づいてクエリセットをフィルタリング
        return filter_transactions_by_user(request.user, qs)

    #def has_view_permission(self, request, obj=None):
    #    if obj is not None:
    #        check_transaction_access(request.user, obj)
    #        return True
    #    return super().has_view_permission(request, obj)


class WalletTransactionInline(admin.TabularInline):
    model = WalletTransaction
    extra = 0
    fields = ('transaction_type', 'amount', 'balance', 'transaction', 'created_at')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ('transaction_type', 'amount', 'balance', 'transaction_link', 'created_at', 'user_email')
    readonly_fields = ('created_at',)
    ordering = ('-created_at',)
    list_filter = ('transaction_type',)
    search_fields = ('wallet__user__email',)

    def user_email(self, obj):
        return obj.wallet.user.email if obj.wallet and obj.wallet.user else '不明'
    user_email.short_description = 'ユーザーのメールアドレス'

    def transaction_link(self, obj):
        if obj.transaction:
            url = f"/admin/api_v1/transaction/{obj.transaction.id}/"
            return format_html('<a href="{}">{}</a>', url, obj.transaction)
        return '不明'
    transaction_link.short_description = '関連取引'
    transaction_link.admin_order_field = 'transaction'


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance")
    search_fields = ("user__email",)
    list_filter = ("user",)
    inlines = [WalletTransactionInline]


class CustomUserAdminForm(forms.ModelForm):
    password = forms.CharField(label='パスワード', widget=forms.PasswordInput, required=False)

    class Meta:
        model = CustomUser
        fields = ('email', 'password', 'user_type', 'is_staff', 'is_superuser', "groups",)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    form = CustomUserAdminForm
    list_display = ("pk", "email", "user_type", "is_staff", "is_superuser")
    search_fields = ("email",)
    readonly_fields = ("last_login", "is_superuser")
    list_filter = ("user_type", "is_staff", "is_superuser")

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None:
            form.base_fields['password'].required = True
        else:
            form.base_fields.pop('password', None)
        return form

    def get_inlines(self, request, obj=None):
        if obj:
            return [StaffInline] if obj.user_type == 'staff' else [CustomerInline]
        return []

    def save_model(self, request, obj, form, change):
        if not change:  # 新規作成の場合
            password = form.cleaned_data.get('password')
            if password:
                obj.set_password(password)  # パスワードをハッシュ化して保存
            
            # スーパーユーザーの場合は自動的にユーザータイプをstaffに設定
            if obj.is_superuser:
                obj.user_type = 'staff'

        # まずCustomUserを保存
        obj.save()

        # ユーザータイプによる情報の自動生成
        if obj.user_type == 'staff':
            staff_profile, created = Staff.objects.get_or_create(user=obj)
            if created:
                staff_profile.staff_code = '初期コード'
                staff_profile.name = 'スタッフ名'
                staff_profile.affiliate_store = None
                staff_profile.permission = None
                staff_profile.save()  # 変更を保存

        elif obj.user_type == 'customer':
            customer_profile, created = Customer.objects.get_or_create(user=obj)
            if created:
                customer_profile.name = '顧客名'
                customer_profile.phone_number = ''
                customer_profile.address = ''
                customer_profile.save()

        # ユーザータイプが変更された場合、元の関連テーブルから情報を削除
        if change:
            if obj.user_type != form.initial['user_type']:
                if form.initial['user_type'] == 'staff':
                    Staff.objects.filter(user=obj).delete()  # 既存のスタッフ情報を削除
                elif form.initial['user_type'] == 'customer':
                    Customer.objects.filter(user=obj).delete()  # 既存の顧客情報を削除


class StaffInline(admin.StackedInline):
    model = Staff
    extra = 0
    verbose_name = "スタッフ情報"
    verbose_name_plural = "スタッフ情報"


class CustomerInline(admin.StackedInline):
    model = Customer
    extra = 0
    verbose_name = "顧客情報"
    verbose_name_plural = "顧客情報"


@admin.register(Staff)
class StaffAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "affiliate_store")
    search_fields = ("name", "user__email")
    list_filter = ("affiliate_store",)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("user", "name")
    search_fields = ("name", "user__email")


@admin.register(UserPermission)
class UserPermissionAdmin(admin.ModelAdmin):
    list_display = (
        "role_name", "register_permission", "global_permission",
        "change_price_permission", "void_permission", "stock_receive_permission"
    )


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ("user", "approval_number", "is_used")
    fields = ("user", "approval_number", "created_at", "is_used")
    readonly_fields = ("created_at",)
    search_fields = ("user__email",)
    list_filter = ("user", "created_at")


class ReturnDetailInline(admin.TabularInline):  # または StackedInline
    model = ReturnDetail
    extra = 0  # 空のフォームを表示しない
    readonly_fields = ("jan", "name", "price", "tax", "discount", "quantity")  # 返品内容は変更不可
    can_delete = False  # インライン上で削除できないように設定


class ReturnPaymentInline(admin.TabularInline):  # または StackedInline
    model = ReturnPayment
    extra = 0
    readonly_fields = ("return_transaction", "payment_method", "amount")  # 必要に応じて読み取り専用フィールドを指定
    can_delete = False  # 支払い内容の削除を防ぐ


@admin.register(ReturnTransaction)
class ReturnTransactionAdmin(admin.ModelAdmin):
    list_display = ("pk", "return_type", "origin_transaction_links", "return_date", "staff_code", "receipt_button")
    fields = (("id", "return_type"), "return_date", ("origin_transaction",  "modify_id"), "store_code", "terminal_id", "staff_code", "reason", "restock",)
    list_display_links = ("pk", "origin_transaction_links",)
    search_fields = ("pk",)
    list_filter = ("return_type", "return_date",)
    readonly_fields = ("id", "origin_transaction", "return_date", "staff_code", "reason")  # 必要に応じて追加
    inlines = [ReturnDetailInline, ReturnPaymentInline]  # インラインで関連データを表示

    def origin_transaction_links(self, obj):
        """外部キーのリンクを生成するメソッド"""
        url = reverse('admin:api_v1_transaction_change', args=[obj.origin_transaction.id])
        return format_html('<a href="{}">{}</a>', url, obj.origin_transaction)

    origin_transaction_links.short_description = 'Origin Transaction'  # 列のヘッダー名を設定

    def receipt_button(self, obj):
        return format_html('<a class="button" href="{}">レシート</a>', reverse('generate_receipt_view', args=[obj.id, 'return']))
    receipt_button.short_description = 'レシートを表示'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # ユーザーのアクセス権限に基づいてクエリセットをフィルタリング
        return filter_transactions_by_user(request.user, qs)

    def has_add_permission(self, request):
        """新規作成を許可しない"""
        return False

    def has_delete_permission(self, request, obj=None):
        """削除を許可しない"""
        return False


# ブラックリストトークンの管理
class BlacklistedTokenAdmin(DefaultBlacklistedTokenAdmin):
    actions = ["delete_expired_blacklisted_tokens"]

    def delete_expired_blacklisted_tokens(self, request, queryset):
        expired_tokens = queryset.filter(expires_at__lt=timezone.now())
        count = expired_tokens.count()
        expired_tokens.delete()
        self.message_user(request, f"{count} の期限切れのブラックリストトークンを削除しました。")
    delete_expired_blacklisted_tokens.short_description = "期限切れのブラックリストトークンを削除"


class DepartmentAdminForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = '__all__'
    
    def __init__(self, *args, **kwargs):
        super(DepartmentAdminForm, self).__init__(*args, **kwargs)
        # level の値を取得して上位部門の queryset を絞る
        if self.instance and self.instance.pk:
            level = self.instance.level
        else:
            # POST データから level を取得（編集時以外）
            level = self.data.get('level')
        
        if level == 'middle':
            # 中分類の場合：親は大分類のみ
            self.fields['parent'].queryset = Department.objects.filter(level='big')
        elif level == 'small':
            # 小分類の場合：親は中分類のみ
            self.fields['parent'].queryset = Department.objects.filter(level='middle')
        else:
            # 大分類の場合は上位部門不要
            self.fields['parent'].queryset = Department.objects.none()
            self.fields['parent'].required = False


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    form = DepartmentAdminForm
    list_display = ('department_code', 'name', 'level', 'parent', 'tax_rate', 'tax_rate_mod_flag', 'discount_flag', 'accounting_flag')
    list_filter = ('level', 'tax_rate', 'discount_flag', 'accounting_flag')
    search_fields = ('code', 'name',)
    ordering = ('level', 'parent__code', 'code')
    fieldsets = (
        (None, {
            'fields': ('level', 'parent', 'code', 'name'),
            'description': "※大分類の場合は、上位部門は不要です。中・小分類の場合は必ず上位部門を選択してください。"
        }),
        ('標準仕様設定', {
            'fields': ('tax_rate', 'tax_rate_mod_flag', 'discount_flag', 'accounting_flag'),
            'description': "標準消費税率は『上位部門引継』、0%、8%、10%から選択してください。税率が8%の場合のみ税率変更フラグが有効となります。各フラグは『上位部門引継』『許可』『禁止』から選択（デフォルトは『上位部門引継』）します。大分類では『上位部門引継』は選択できません。"
        }),
    )


@admin.register(POSA)
class POSAAdmin(admin.ModelAdmin):
    list_display = ('code', 'status', 'is_variable', 'card_value', 'expiration_date', 'buyer', 'user')
    search_fields = ('code',)
    list_filter = ('status', 'posa_type', 'is_variable')
    ordering = ('expiration_date',)
    readonly_fields = ('code', 'expiration_date')
    
    # アクションの追加
    actions = ['delete_expired_posas', 'delete_activated_posas']
    
    def save_model(self, request, obj, form, change):
        # 新規追加時にPOSAコードを自動生成
        if not change:  # 新規追加時
            obj.code = utils.generate_unique_posa_code()
        super().save_model(request, obj, form, change)
    
    def formfield_for_dbfield(self, db_field, **kwargs):
        # POSAコードフィールドのラベルを変更
        formfield = super().formfield_for_dbfield(db_field, **kwargs)
        if db_field.name == 'code':
            formfield.label = "POSAコード (自動生成)"
            formfield.help_text = "新規追加時は自動的に生成されます"
        return formfield
    
    def delete_expired_posas(self, request, queryset):
        """期限切れのPOSAを削除するアクション"""
        today = timezone.localdate()
        expired_posas = queryset.filter(expiration_date__lt=today)
        count = expired_posas.count()
        if count > 0:
            expired_posas.delete()
            self.message_user(request, f"{count}件の期限切れPOSAカードを削除しました。")
        else:
            self.message_user(request, "期限切れのPOSAカードはありませんでした。", level=messages.WARNING)
    delete_expired_posas.short_description = "選択した期限切れPOSAを削除"
    
    def delete_activated_posas(self, request, queryset):
        """利用済みのPOSAを削除するアクション"""
        activated_posas = queryset.filter(status='charged')
        count = activated_posas.count()
        if count > 0:
            activated_posas.delete()
            self.message_user(request, f"{count}件のアクティベート済みPOSAカードを削除しました。")
        else:
            self.message_user(request, "アクティベート済みのPOSAカードはありませんでした。", level=messages.WARNING)
    delete_activated_posas.short_description = "選択した利用済POSAを削除"


class BulkGeneratePOSACodesForm(forms.ModelForm):
    quantity = forms.IntegerField(
        label="発行枚数",
        min_value=1,
        max_value=100,
        help_text="1〜100の範囲で指定",
        initial=1  # デフォルト値を1に設定
    )

    class Meta:
        model = apps.get_model('api_v1', 'BulkGeneratePOSACodes')  # プロキシモデルを指定
        fields = ["posa_type", "is_variable", "card_value", "quantity"]
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # POSA種別のデフォルト値を「ウォレットギフトカード」に設定
        if 'posa_type' in self.fields:
            self.fields['posa_type'].initial = 'wallet_gift'


@admin.register(apps.get_model('api_v1', 'BulkGeneratePOSACodes'))
class BulkGeneratePOSACodesAdmin(admin.ModelAdmin):
    form = BulkGeneratePOSACodesForm

    def get_queryset(self, request):
        return super().get_queryset(request).none()  # 一覧は空に

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return self.has_add_permission(request)

    fields = ("posa_type", "is_variable", "card_value", "quantity")

    def save_model(self, request, obj, form, change):
        # フォームから直接データを取得
        data = form.cleaned_data
        utils.bulk_generate_posa_codes(
            posa_type=data["posa_type"],
            is_variable=data["is_variable"],
            card_value=data["card_value"],
            quantity=data["quantity"]
        )
        self.message_user(request, f"POSAコードを {data['quantity']} 件発行しました。")
        
    def response_add(self, request, obj, post_url_continue=None):
        """
        Override the redirect after the 'Save' button is pressed when adding
        """
        # 一括発行後は POSA 一覧ページに移動する
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        return HttpResponseRedirect(reverse('admin:api_v1_posa_changelist'))

    def get_urls(self):
        """
        一覧表示をスキップして直接追加画面に遷移するようにURLをカスタマイズ
        """
        from django.urls import path
        from functools import update_wrapper
        
        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            wrapper.model_admin = self
            return update_wrapper(wrapper, view)
        
        # 元のurlsを取得
        urls = super().get_urls()
        
        # 追加画面へのURLパターンを作成
        custom_urls = [
            path('', wrap(self.add_view), name='api_v1_bulkgenerateposacodes_changelist'),
        ]
        
        # カスタムURLを元のURLの前に追加して返す（順序が重要）
        return custom_urls + urls


# 有効なトークンの管理
class OutstandingTokenAdmin(DefaultOutstandingTokenAdmin):
    actions = ["delete_expired_outstanding_tokens"]
    readonly_fields = ("user", "token", "created_at", "expires_at")

    def delete_expired_outstanding_tokens(self, request, queryset):
        expired_tokens = queryset.filter(expires_at__lt=timezone.now())
        count = expired_tokens.count()
        expired_tokens.delete()
        self.message_user(request, f"{count} の期限切れの有効トークンを削除しました。")

    delete_expired_outstanding_tokens.short_description = "期限切れの有効トークンを削除"

    def has_add_permission(self, request):
        return False  # 新規作成を禁止

    def has_change_permission(self, request, obj=None):
        return False  # 変更を禁止

    def has_delete_permission(self, request, obj=None):
        return True  # 削除を許可


admin.site.unregister(BlacklistedToken)
admin.site.unregister(OutstandingToken)

admin.site.register(BlacklistedToken, BlacklistedTokenAdmin)
admin.site.register(OutstandingToken, OutstandingTokenAdmin)

from collections import defaultdict
from datetime import date

@admin.register(DailySalesReport)
class DailySalesReportAdmin(admin.ModelAdmin):
    change_list_template = 'admin/daily_sales_report.html'

    def changelist_view(self, request, extra_context=None):
        # --- 権限に基づく店舗リストの作成 ---
        user = request.user
        has_full_permission = user.is_superuser or (hasattr(user, 'staff_profile') and user.staff_profile.permission and user.staff_profile.permission.global_permission)
        
        if has_full_permission:
            allowed_stores = Store.objects.all()
        else:
            if hasattr(user, 'staff_profile') and user.staff_profile.affiliate_store:
                allowed_stores = Store.objects.filter(store_code=user.staff_profile.affiliate_store.store_code)
            else:
                allowed_stores = Store.objects.none()

        # --- GETパラメータの取得 ---
        start_date_str = request.GET.get('start_date')
        end_date_str = request.GET.get('end_date')
        selected_store_code = request.GET.get('store_code', 'all')

        # 日付の決定
        if start_date_str and end_date_str:
            start_date = date.fromisoformat(start_date_str)
            end_date = date.fromisoformat(end_date_str)
        else:
            start_date = end_date = timezone.now().date()

        # --- クエリセットのフィルタリング ---
        sales_details = TransactionDetail.objects.filter(transaction__date__date__range=(start_date, end_date))
        return_details = ReturnDetail.objects.filter(return_transaction__return_date__date__range=(start_date, end_date))

        if selected_store_code != 'all':
            sales_details = sales_details.filter(transaction__store_code=selected_store_code)
            # 返品は元の販売店舗でフィルタリング
            return_details = return_details.filter(return_transaction__origin_transaction__store_code=selected_store_code)

        # --- 集計ロジック ---
        department_summary = defaultdict(lambda: defaultdict(int))
        store_summary = defaultdict(lambda: defaultdict(int))
        product_summary = defaultdict(lambda: defaultdict(int))
        payment_summary = defaultdict(lambda: defaultdict(int))

        # 1. 売上集計
        for detail in sales_details.select_related('transaction__store_code'):
            # JANから商品情報を取得
            try:
                product = Product.objects.get(jan=detail.jan)
                dep_name = product.department_code.name if product.department_code else '部門未設定'
            except Product.DoesNotExist:
                dep_name = '商品マスター未登録'

            store_name = detail.transaction.store_code.name
            
            # 金額と数量
            amount = detail.price * detail.quantity
            quantity = detail.quantity
            discount = detail.discount

            # 各サマリーに加算
            department_summary[dep_name]['sales'] += amount
            department_summary[dep_name]['quantity'] += quantity
            store_summary[store_name]['sales'] += amount
            store_summary[store_name]['quantity'] += quantity
            store_summary[store_name]['discount'] += discount
            product_summary[detail.jan]['sales'] += amount
            product_summary[detail.jan]['quantity'] += quantity
            product_summary[detail.jan]['discount'] += discount
            product_summary[detail.jan]['name'] = detail.name


        # 2. 返品集計
        for detail in return_details.select_related('return_transaction__origin_transaction__store_code'):
            try:
                product = Product.objects.get(jan=detail.jan)
                dep_name = product.department_code.name if product.department_code else '部門未設定'
            except Product.DoesNotExist:
                dep_name = '商品マスター未登録'
            
            # 元の販売店舗で集計
            store_name = detail.return_transaction.origin_transaction.store_code.name
            
            amount = detail.price * detail.quantity
            quantity = detail.quantity
            discount = detail.discount

            department_summary[dep_name]['returns'] += amount
            department_summary[dep_name]['return_quantity'] += quantity
            store_summary[store_name]['returns'] += amount
            store_summary[store_name]['return_quantity'] += quantity
            store_summary[store_name]['return_discount'] += discount
            product_summary[detail.jan]['returns'] += amount
            product_summary[detail.jan]['return_quantity'] += quantity
            product_summary[detail.jan]['return_discount'] += discount

        # 3. 支払い方法集計
        payments = Payment.objects.filter(transaction__date__date__range=(start_date, end_date))
        return_payments = ReturnPayment.objects.filter(return_transaction__return_date__date__range=(start_date, end_date))

        if selected_store_code != 'all':
            payments = payments.filter(transaction__store_code=selected_store_code)
            return_payments = return_payments.filter(return_transaction__origin_transaction__store_code=selected_store_code)

        for p in payments:
            payment_summary[p.get_payment_method_display()]['amount'] += p.amount
        for rp in return_payments:
            payment_summary[rp.get_payment_method_display()]['amount'] -= rp.amount


        # --- 全体サマリー計算 ---
        total_sales = sum(s['sales'] for s in store_summary.values())
        total_returns = sum(s['returns'] for s in store_summary.values())
        total_discount = sum(s['discount'] for s in store_summary.values())
        net_sales = total_sales - total_returns
        discount_rate = (total_discount / total_sales * 100) if total_sales > 0 else 0

        # --- 純計算と最終的なコンテキストの準備 ---
        def calculate_net_and_rates(summary, is_product=False):
            for k, v in summary.items():
                v['net_sales'] = v['sales'] - v['returns']
                v['net_quantity'] = v['quantity'] - v['return_quantity']
                if is_product:
                    v['net_discount'] = v.get('discount', 0) - v.get('return_discount', 0)
                    v['discount_rate'] = (v['net_discount'] / v['sales'] * 100) if v['sales'] > 0 else 0
            return summary

        department_summary = calculate_net_and_rates(department_summary)
        store_summary = calculate_net_and_rates(store_summary)
        product_summary = calculate_net_and_rates(product_summary, is_product=True)

        context = self.admin_site.each_context(request)
        context.update({
            'start_date': start_date,
            'end_date': end_date,
            'allowed_stores': allowed_stores,
            'has_full_permission': has_full_permission,
            'selected_store_code': selected_store_code,
            'department_sales': sorted([{'name': k, **v} for k, v in department_summary.items()], key=lambda x: x['net_sales'], reverse=True),
            'store_sales': sorted([{'name': k, **v} for k, v in store_summary.items()], key=lambda x: x['net_sales'], reverse=True),
            'product_sales': sorted([{'jan': k, **v} for k, v in product_summary.items()], key=lambda x: x['net_sales'], reverse=True),
            'payment_summary': sorted([{'method': k, **v} for k, v in payment_summary.items()], key=lambda x: x['amount'], reverse=True),
            'total_summary': {
                'net_sales': net_sales,
                'total_discount': total_discount,
                'discount_rate': discount_rate,
            },
            'opts': self.model._meta,
            'title': '売上レポート',
        })
        
        return TemplateResponse(request, self.change_list_template, context)

# 管理画面のタイトル設定
admin.site.site_header = "商品管理システム"
admin.site.index_title = "管理画面"
admin.site.site_title = "管理者"
admin.site.site_url = "/api/"
