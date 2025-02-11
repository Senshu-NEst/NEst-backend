import rules
from django.core.exceptions import PermissionDenied

# ① 取引閲覧権限の判定用プレディケート
@rules.predicate
def can_view_transaction(user, transaction):
    # スーパーユーザーなら無条件アクセス
    if user.is_superuser:
        return True

    # ユーザーにスタッフ情報が存在しない場合はアクセス不可
    try:
        staff = user.staff_profile
    except AttributeError:
        return False

    # スタッフ権限が設定されていない場合はアクセス不可
    if not (staff.permission and isinstance(staff.permission, object)):
        return False

    # global_permission があれば無条件アクセス
    if staff.permission.global_permission:
        return True

    # それ以外は、取引の店舗コードとスタッフの所属店舗が一致していることを確認
    return transaction.store_code == staff.affiliate_store

# ② 上記プレディケートを rules に登録
rules.add_rule('view_transaction', can_view_transaction)

# ③ 権限がない場合に403エラーを発生させる関数
def check_transaction_access(user, transaction):
    """
    ユーザーが指定の取引にアクセスできるかを判定し、アクセス不可の場合は
    403エラー（PermissionDenied）を発生させる。
    """
    if not rules.test_rule('view_transaction', user, transaction):
        raise PermissionDenied("この取引にアクセスする権限がありません。")
    return True

# ④ クエリセットのフィルタリング用共通関数
def filter_transactions_by_user(user, queryset):
    """
    ユーザーのアクセス権限に応じて、取引クエリセットをフィルタリングする。
    スーパーユーザーまたは global_permission を持つ場合はフィルタリングせず、
    それ以外はユーザーの所属店舗に一致する取引のみを返す。
    """
    # スーパーユーザーまたは global_permission がある場合はフィルタ不要
    if user.is_superuser:
        return queryset
    try:
        staff = user.staff_profile
    except AttributeError:
        # スタッフ情報がない場合は空のクエリセットを返す
        return queryset.none()
    if staff.permission and staff.permission.global_permission:
        return queryset
    # それ以外は所属店舗と一致する取引のみフィルタリング
    return queryset.filter(store_code=staff.affiliate_store)

## ⑤ 他の操作（変更・削除）についても同様にプレディケートを定義可能
#@rules.predicate
#def can_change_transaction(user, transaction):
#    if user.is_superuser:
#        return True
#    try:
#        staff = user.staff_profile
#    except AttributeError:
#        return False
#    if staff.permission and staff.permission.global_permission:
#        return True
#    # ここでは、変更操作は閲覧と同じ店舗チェックとする（必要に応じて拡張可能）
#    return transaction.store_code == staff.affiliate_store

#rules.add_rule('change_transaction', can_change_transaction)

#def check_change_transaction(user, transaction):
#    if not rules.test_rule('change_transaction', user, transaction):
#        raise PermissionDenied("この取引を変更する権限がありません。")
#    return True
