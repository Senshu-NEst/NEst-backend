from django.contrib import admin
from django import forms
from .models import Product, Store, Stock, Transaction, TransactionDetail, CustomUser, UserPermission, StockReceiveHistory, StockReceiveHistoryItem, StorePrice, Payment, ProductVariation, ProductVariationDetail
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.admin import (
    BlacklistedTokenAdmin as DefaultBlacklistedTokenAdmin,
    OutstandingTokenAdmin as DefaultOutstandingTokenAdmin
)
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken


class NegativeStockFilter(admin.SimpleListFilter):
    title = '在庫数'
    parameter_name = 'negative_stock'

    def lookups(self, request, model_admin):
        return (
            ('negative', 'マイナス在庫'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'negative':
            return queryset.filter(stock__lt=0)
        return queryset


class TransactionDetailInline(admin.TabularInline):
    model = TransactionDetail
    extra = 0  # 商品を追加するための空行数
    verbose_name = "取引詳細"
    verbose_name_plural = "取引詳細"


class PaymentDetailInline(admin.TabularInline):
    model = Payment
    extra = 0  # 商品を追加するための空行数
    verbose_name = "支払"
    verbose_name_plural = "支払詳細"


class StockInline(admin.TabularInline):
    model = Stock
    extra = 0  # 新しい在庫を追加するための空行数
    verbose_name = "在庫"
    verbose_name_plural = "在庫情報"


class VariationDetailInline(admin.TabularInline):
    model = ProductVariationDetail
    extra = 1  # 新しいバリエーションを追加するための空行数
    verbose_name = "商品色名"
    verbose_name_plural = "商品色名一覧"

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("jan", "name", "price", "tax", "status")
    search_fields = ("name", "jan")
    list_filter = ("status", "tax")
    inlines = [StockInline]


@admin.register(ProductVariation)
class ProductVariationAdmin(admin.ModelAdmin):
    list_display = ("instore_jan", "name")
    search_fields = ("instore_jan", "name")
    inlines = [VariationDetailInline]  # 色名を関連づけて表示させる


@admin.register(StorePrice)
class StorePriceAdmin(admin.ModelAdmin):
    list_display = ("store_code", "jan", "jan__name", "price")
    search_fields = ("store_code", "jan")
    list_filter = ("store_code",)


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("store_code", "name")
    search_fields = ("name", "store_code")

    def get_readonly_fields(self, request, obj=None):
        # 新規店舗追加時はstore_codeを編集可能にする
        if obj is None:
            return []
        return ("store_code",)  # 既存の店舗の場合はstore_codeをreadonlyにする


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    readonly_fields = ("updated_at",)
    list_display = ("store_code", "jan", "jan__name", "stock")
    search_fields = ("jan__name", "jan__jan")
    list_filter = ("store_code", NegativeStockFilter)  # カスタムフィルターを適用


class StockReceiveHistoryItemInline(admin.TabularInline):
    model = StockReceiveHistoryItem
    extra = 0  # 商品を追加するための空行数
    verbose_name = "入荷"
    verbose_name_plural = "入荷商品"


@admin.register(StockReceiveHistory)
class StockReceiveHistoryAdmin(admin.ModelAdmin):
    list_display = ("received_at", "store_code__store_code", "staff_code__name")
    search_fields = ()
    list_filter = ("received_at", "store_code", "staff_code__name", "store_code")
    inlines = [StockReceiveHistoryItemInline]  # 入荷した商品を紐づける


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "store_code", "staff_code", "status", "total_amount")
    search_fields = ("id", "store_code__store_code", "staff_code__staff_code")
    list_filter = ("status", "date", "staff_code__name", "store_code")
    inlines = [PaymentDetailInline, TransactionDetailInline]  # 支払い方法と購入商品を紐づける


class CustomUserAdminForm(forms.ModelForm):
    password = forms.CharField(
        label='パスワード',
        widget=forms.PasswordInput,
        required=False
    )

    class Meta:
        model = CustomUser
        fields = ('staff_code', 'name', 'affiliate_store', 'password', 'is_staff', 'is_superuser', 'permission')


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    form = CustomUserAdminForm
    list_display = ("staff_code", "name", "is_staff", "is_superuser", "affiliate_store")
    search_fields = ("staff_code", "name")
    list_filter = ("is_staff", "is_superuser", "affiliate_store")

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if obj is None:  # 新規作成時
            form.base_fields['password'].required = True  # 新規作成時はパスワードを必須にする
        else:
            form.base_fields.pop('password', None)  # 既存ユーザーの場合はパスワードフォームを表示させない
        return form

    def save_model(self, request, obj, form, change):
        if not change:  # 新規作成の場合
            password = form.cleaned_data.get('password')
            if password:
                obj.set_password(password)  # パスワードをハッシュ化して保存
        obj.save()


@admin.register(UserPermission)
class UserPermissionAdmin(admin.ModelAdmin):
    list_display = (
        "role_name", "register_permission", "global_permission",
        "change_price_permission", "void_permission", "stock_receive_permission"
    )


# ブラックリストトークンの管理
class BlacklistedTokenAdmin(DefaultBlacklistedTokenAdmin):
    actions = ["delete_expired_blacklisted_tokens"]

    def delete_expired_blacklisted_tokens(self, request, queryset):
        expired_tokens = queryset.filter(expires_at__lt=timezone.now())
        count = expired_tokens.count()
        expired_tokens.delete()
        self.message_user(request, f"{count} の期限切れのブラックリストトークンを削除しました。")

    delete_expired_blacklisted_tokens.short_description = "期限切れのブラックリストトークンを削除"


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
