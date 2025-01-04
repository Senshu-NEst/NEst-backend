from django.contrib import admin
from .models import Product, Store, Stock, Transaction, TransactionDetail, CustomUser, UserPermission, StockReceiveHistory, StockReceiveHistoryItem, StorePrice
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.admin import BlacklistedTokenAdmin as DefaultBlacklistedTokenAdmin
from rest_framework_simplejwt.token_blacklist.admin import OutstandingTokenAdmin as DefaultOutstandingTokenAdmin
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


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("jan", "name", "price", "tax", "status")
    search_fields = ("name", "jan")
    list_filter = ("status",)


@admin.register(StorePrice)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("store_code", "jan", "price",)
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
    list_display = ("store_code", "jan", "stock")
    search_fields = ("jan__name", "jan__jan")
    list_filter = ("store_code", NegativeStockFilter)  # カスタムフィルターを適用


class StockReceiveHistoryItemInline(admin.TabularInline):
    model = StockReceiveHistoryItem
    extra = 0  # 商品を追加するための空行数


@admin.register(StockReceiveHistory)
class StockReceiveHistoryAdmin(admin.ModelAdmin):
    list_display = ("received_at", "store_code", "staff_code")
    search_fields = ("store_code__store_code", "staff_code__staff_code")
    inlines = [StockReceiveHistoryItemInline]  # 入荷した商品を関連づけて表示させる


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "store_code", "staff_code", "status", "total_amount")
    search_fields = ("id", "store_code__store_code", "staff_code__staff_code")
    list_filter = ("status", "date", "store_code")
    inlines = [TransactionDetailInline]  # 購入商品を関連づけて表示させる


@admin.register(TransactionDetail)
class TransactionDetailAdmin(admin.ModelAdmin):
    list_display = ("transaction", "jan", "name", "price", "quantity")
    search_fields = ("transaction__id", "jan__name")
    list_filter = ("transaction",)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    readonly_fields =("staff_code","last_login",)
    exclude = ("password",)
    list_display = ("staff_code", "name", "permission", "is_superuser", "is_staff", "affiliate_store",)
    search_fields = ("staff_code", "name")
    list_filter = ("is_staff", "is_superuser", "affiliate_store")


@admin.register(UserPermission)
class UserPermissionAdmin(admin.ModelAdmin):
    list_display = ("role_name", "register_permission", "global_permission", "change_price_permission", "void_permission", "stock_receive_permission")


# ブラックリストトークンの管理
class BlacklistedTokenAdmin(DefaultBlacklistedTokenAdmin):
    actions = ["delete_expired_blacklisted_tokens"]

    def delete_expired_blacklisted_tokens(self, request, queryset):
        expired_tokens = queryset.filter(expires_at__lt=timezone.now())
        count = expired_tokens.count()
        expired_tokens.delete()
        self.message_user(request, f"{count} expired blacklisted tokens deleted.")

    delete_expired_blacklisted_tokens.short_description = "Delete expired blacklisted tokens"


# 有効なトークンの管理
class OutstandingTokenAdmin(DefaultOutstandingTokenAdmin):
    actions = ["delete_expired_outstanding_tokens"]
    readonly_fields = ("user", "token", "created_at", "expires_at")  # 読み取り専用フィールド

    def delete_expired_outstanding_tokens(self, request, queryset):
        expired_tokens = queryset.filter(expires_at__lt=timezone.now())
        count = expired_tokens.count()
        expired_tokens.delete()
        self.message_user(request, f"{count} expired outstanding tokens deleted.")

    delete_expired_outstanding_tokens.short_description = "Delete expired outstanding tokens"

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
