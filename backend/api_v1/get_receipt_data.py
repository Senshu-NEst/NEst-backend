from django.utils import timezone
from .models import WalletTransaction, Transaction, Payment


def get_wallet_info(transaction_id):
    
    # 取引IDに基づいてウォレットトランザクションを取得
    wallet_transactions = WalletTransaction.objects.filter(transaction_id=transaction_id)

    if not wallet_transactions.exists():
        return None  # トランザクションが存在しない場合はNoneを返す

    # 最初のトランザクションからbalanceとamountを取得
    latest_transaction = wallet_transactions.last()
    balance = latest_transaction.balance
    amount = latest_transaction.amount

    # ユーザーのIDを取得
    user = latest_transaction.wallet.user

    # 最終利用日を取得（前回の取引の日付）
    previous_transaction = Transaction.objects.filter(user=user).exclude(id=transaction_id).order_by('-date').first()

    if previous_transaction:
        last_used_date = previous_transaction.date
    else:
        last_used_date = None  # 前回の取引が存在しない場合

    return {
        "pre_balance": balance,
        "last_used_date": last_used_date,
        "amount": amount
    }


def generate_receipt_text(transaction):
    staff_code = transaction.staff_code.staff_code
    staff_name = transaction.staff_code.name
    user = transaction.user  # ユーザーオブジェクトを取得
    user_id = user.id  # ユーザーIDを取得
    user_id_hyphenated = f"{user_id[:2]}-{user_id[2:6]}-{user_id[6:10]}-{user_id[10:14]}-{user_id[14:18]}-{user_id[18:22]}-{user_id[22:]}"
    terminal_id = transaction.terminal_id
    sale_products = transaction.sale_products.all()
    sale_id = transaction.id
    if transaction.status == "resale":
        resale = "*"

    receipt = f"""{{image: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}}
| {transaction.store_code.name} |

|{transaction.date.strftime('%Y年%m月%d日 %H:%M')}
|担当:{staff_code}{staff_name}
|取引ID:{terminal_id}-{sale_id}{resale}
{{border:none}}
{{border:line; width:22}}
^領 収 書
{{border:space; width:3,*,8; text:nowrap}}
"""

    for product in sale_products:
        jan_last_3_digits = product.jan.jan[-3:]
        is_reduced_tax = "*" if product.tax == 8 else "ﾋ" if product.tax == 0 else "~"

        if product.quantity == 1:
            if product.discount > 0:
                # 1点のみ、値引きあり → 2行
                receipt += f"{jan_last_3_digits} |{product.name} | ¥{product.price:,}{is_reduced_tax}\n"
                receipt += f"  ||~~~~~Δ値引 @\-{product.discount:,} | ¥Δ{product.discount:,}{is_reduced_tax}\n"
            else:
                # 1点のみ、値引きなし → 1行
                receipt += f"{jan_last_3_digits} |{product.name} | ¥{product.price:,}{is_reduced_tax}\n"
        else:
            product_total_price = product.price * product.quantity  # 合計金額
            if product.discount > 0:
                # 複数点、値引きあり → 3行
                receipt += f"{jan_last_3_digits} |{product.name}\n"

                receipt += f"  ||~~~~~@{product.price:,}*{product.quantity} | ¥{product_total_price}{is_reduced_tax}\n"
                total_discount = product.discount * product.quantity  # 合計値引き額
                receipt += f"  ||~~~~~Δ値引 @\-{product.discount:,}*{product.quantity} | ¥Δ{total_discount:,}{is_reduced_tax}\n"
            else:
                # 複数点、値引きなし → 2行
                receipt += f"{jan_last_3_digits} |{product.name}\n"
                receipt += f"  ||~~~~~@{product.price:,}*{product.quantity} | ¥{product_total_price}{is_reduced_tax}\n"
    if transaction.discount_amount > 0:
        before_discount_total = transaction.total_amount + transaction.discount_amount
        receipt += f"""{{width:*,*}}\n
        |(値引前小計 | ¥{before_discount_total:,})
        |(値引合計 | ¥Δ{transaction.discount_amount:,})\n"""

    receipt += f"""-
{{width:*,20}}
|^合計 | ^¥{transaction.total_amount:,}
{{width:auto}}
| (10％税額 | ¥{transaction.total_tax10:,})
| ( 8％税額 | ¥{transaction.total_tax8:,})
| (合計消費税 | ¥{transaction.tax_amount:,})
|点数 | {transaction.total_quantity}点

|^お支払い
"""

    # 支払い方法の情報を追加
    for payment in transaction.payments.all():
        payment_method_display = dict(Payment.PAYMENT_METHOD_CHOICES).get(payment.payment_method, payment.payment_method)
        receipt += f"|{payment_method_display} | ¥{payment.amount:,}\n"

    receipt += f"""|^釣銭 | ¥{transaction.change:,}
{{width: *}}
|「*」は軽減税率対象商品です。
-
"""

    # ユーザー情報とウォレット情報を追加
    wallet = getattr(user, 'wallet', None)  # ユーザーのウォレットを取得

    # ユーザーIDを追加
    receipt += f"""{{width: *}}
    |UID:
    |{user_id_hyphenated}
    {{width:12,*}}
    """

    # ウォレットが存在するか確認
    if wallet:
        # ユーザーのウォレットに関連するウォレットトランザクションを取得し、今回の取引 ID より前のトランザクションをフィルタリング
        wallet_transactions = WalletTransaction.objects.filter(wallet=wallet, created_at__lt=transaction.date).order_by('-created_at')

        # 最も最近のトランザクションを前回の取引として取得
        previous_wallet_transaction = wallet_transactions.first()

        # 今回の取引に関連するウォレットトランザクションを取得
        current_wallet_transaction = WalletTransaction.objects.filter(transaction__id=sale_id, wallet=wallet).first()

        if previous_wallet_transaction and current_wallet_transaction:
            last_used_date = previous_wallet_transaction.created_at.strftime('%Y年%m月%d日')  # 最終利用日を取得
            pre_balance = previous_wallet_transaction.balance  # 利用前残高を取得
            used_amount = current_wallet_transaction.amount  # 今回利用金額を取得
            post_balance = current_wallet_transaction.balance  # 利用後残高を取得

            # 最終利用日、利用前残高、今回利用金額、利用後残高を追加
            receipt += f"|最終利用日 | {last_used_date}\n"
            receipt += f"|利用前残高 | ¥{pre_balance:,}\n"
            receipt += f"|今回利用金額 | ¥{used_amount:,}\n"
            receipt += f"|利用後残高 | ¥{post_balance:,}\n"
        else:
            # 前回の取引または現在の取引が存在しない場合の処理
            receipt += "|最終利用日 | N/A\n"
            receipt += "|利用前残高 | ¥0\n"
            receipt += "|今回利用金額 | ¥0\n"
            receipt += "|利用後残高 | ¥0\n"
    else:
        receipt += "|最終利用日 | N/A\n"
        receipt += "|利用前残高 | ¥0\n"
        receipt += "|今回利用金額 | ¥0\n"
        receipt += "|利用後残高 | ¥0\n"

    receipt += f"-\n"
    # バーコードの挿入
    receipt += f"{{code:{sale_id}; option:code128,3,48,nohri}}\n"

    return receipt







def generate_return_receipt(transaction):
    staff_code = transaction.staffcode.staffcode
    sale_products = transaction.sale_products.all()
    sale_id = transaction.id  # ここを変更

    receipt = f"""
{{image:(base64)}}

|{transaction.storecode}:{transaction.storecode.name}

|{transaction.sale_date.strftime('%Y-%m-%d %H:%M')}
|担当No.{staff_code}
|取引ID:{sale_id}
{{border:none}}
{{border:line; width:22}}
^領 収 書
{{border:space; width:3,*,3,8; text:nowrap}}
"""

    for product in sale_products:
        jan_last_3_digits = product.JAN.JAN[-3:]
        is_reduced_tax = "*" if product.tax == 8 else "~"
        receipt += f"{jan_last_3_digits} |{product.name} | ×{product.points} | ¥{product.price:,}{is_reduced_tax}\n"

    receipt += f"""-
{{width:*,20}}
|^合計 | ^¥{transaction.total_amount:,}
{{width:auto}}
| (10％税額 | ¥{transaction.tax_10_percent:,})
| ( 8％税額 | ¥{transaction.tax_8_percent:,})
| (合計消費税 | ¥{transaction.tax_amount:,})
|点数 | {transaction.purchase_points}点

|^お支払い
|現金 | ¥{transaction.deposit:,}
|^釣銭 | ¥{transaction.change:,}
{{width: *}}
|「*」は軽減税率対象商品です。
-
"""
    if transaction.relation_id:
        receipt += f"|取消取引：{transaction.relation_id}\n"

    # バーコードの挿入
    receipt += f"{{code:{sale_id}; option:code128,3,48,nohri}}\n"

    return receipt