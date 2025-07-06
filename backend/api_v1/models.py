import ulid
from django.utils import timezone
from datetime import timedelta, date
from django.db import models, transaction
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin, Group
from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.validators import MaxValueValidator, MinValueValidator
from .utils import is_valid_jan_code, generate_unique_instore_jan


class BaseModel(models.Model):
    """共通フィールドを持つ抽象モデル"""
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="作成日時")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新日時")

    class Meta:
        abstract = True

    # save前にmodelsのバリデーションチェックを行う
    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


TAX_CHOICES = (
    (0.0, "0% - 免税"),
    (8.0, "8% - 軽減"),
    (10.0, "10% - 通常"),
)


class Product(BaseModel):
    """商品モデル"""
    class Status(models.TextChoices):
        IN_DEAL = "in_deal", "取引中"
        SPOT = "spot", "スポット"
        DISCON = "discon", "終売"

    jan = models.CharField(max_length=13, primary_key=True, verbose_name="JANコード")
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="商品価格")
    tax = models.DecimalField(max_digits=3, decimal_places=1, default=8.0, choices=TAX_CHOICES, verbose_name="消費税率")
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.IN_DEAL, verbose_name="取引状態")
    disable_change_tax = models.BooleanField(default=False, verbose_name="POSでの税率変更を禁止")
    disable_change_price = models.BooleanField(default=False, verbose_name="POSでの価格変更を禁止")
    
    # 部門コードの追加
    department_code = models.ForeignKey(
        'Department',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        limit_choices_to={'level': 'small'},  # 小分類のみを選択肢にする
        verbose_name="部門コード"
    )

    class Meta:
        verbose_name = "商品"
        verbose_name_plural = "商品一覧"

    def __str__(self):
        return self.jan

    def clean(self):
        if not is_valid_jan_code(self.jan):
            raise ValidationError({"jan": "チェックディジットエラー！"})
        self.validate_tax_rate(self.tax)

    def validate_tax_rate(self, tax_rate):
        if tax_rate not in [0, 8, 10]:
            raise ValidationError("税率は0, 8, 10のいずれかでなければなりません。")

    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)
            stores = Store.objects.all()
            existing_stocks = set(Stock.objects.filter(jan=self).values_list('store_code_id', flat=True))

            stock_entries = [
                Stock(store_code=store, jan=self, stock=0)
                for store in stores if store.store_code not in existing_stocks
            ]

            batch_size = 500
            for i in range(0, len(stock_entries), batch_size):
                Stock.objects.bulk_create(stock_entries[i:i + batch_size])


class Store(BaseModel):
    """店舗モデル"""
    store_code = models.CharField(max_length=20, primary_key=True, verbose_name="店番号")
    name = models.CharField(max_length=255, verbose_name="店名")

    class Meta:
        verbose_name = "店舗"
        verbose_name_plural = "店舗一覧"

    def __str__(self):
        return f"{self.store_code}-{self.name}"

    def save(self, *args, **kwargs):
        """新しい店舗が追加された際に全ての商品に対する在庫を生成"""
        super().save(*args, **kwargs)
        products = Product.objects.all()
        stock_entries = [Stock(store_code=self, jan=product, stock=0) for product in products]

        batch_size = 500
        with transaction.atomic():
            for i in range(0, len(stock_entries), batch_size):
                Stock.objects.bulk_create(stock_entries[i:i + batch_size])


class Stock(BaseModel):
    """在庫モデル"""
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="店番号")
    jan = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="JANコード")
    stock = models.IntegerField(default=0, verbose_name="在庫数")

    class Meta:
        unique_together = ("store_code", "jan")
        verbose_name = "在庫"
        verbose_name_plural = "在庫一覧"

    def __str__(self):
        return f"{self.store_code.store_code} - {self.jan}"


class StorePrice(BaseModel):
    """店舗ごとの価格モデル"""
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="店番号")
    jan = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="JANコード")
    price = models.IntegerField(verbose_name="店舗価格")

    class Meta:
        unique_together = ("store_code", "jan")
        verbose_name = "店舗価格"
        verbose_name_plural = "店舗価格一覧"

    def __str__(self):
        return f"{self.store_code} - {self.jan} - {self.price}"

    def clean(self):
        if self.price == self.jan.price:
            raise ValidationError("店舗価格は商品価格と異なる必要があります。")

    def save(self, *args, **kwargs):
        self.clean()  # 保存前にcleanメソッドを呼び出す
        super().save(*args, **kwargs)

    def get_price(self):
        return self.price if self.price is not None else self.jan.price


class ProductVariation(models.Model):
    """インストアJANコードを管理するモデル"""
    instore_jan = models.CharField(primary_key=True, max_length=13, unique=True, editable=False, verbose_name="インストアJANコード")
    name = models.CharField(max_length=50, verbose_name="代表商品名")
    products = models.ManyToManyField(Product, through='ProductVariationDetail', related_name='variations')

    class Meta:
        verbose_name = "商品バリエーション"
        verbose_name_plural = "商品バリエーション一覧"

    def save(self, *args, **kwargs):
        if not self.instore_jan:
            self.instore_jan = generate_unique_instore_jan(ProductVariation.objects.values_list('instore_jan', flat=True))
        super().save(*args, **kwargs)


class ProductVariationDetail(models.Model):
    """中間テーブル：商品とバリエーションの関係を管理するモデル"""
    product_variation = models.ForeignKey(ProductVariation, on_delete=models.CASCADE, related_name='variation_details')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variation_colors')
    color_name = models.CharField(null=True, blank=True, max_length=50, verbose_name="色名")

    class Meta:
        unique_together = ('product_variation', 'product')
        verbose_name = "商品バリエーション詳細"
        verbose_name_plural = "商品バリエーション詳細一覧"

    def __str__(self):
        return f"{self.product.name} - {self.color_name} ({self.product_variation.instore_jan})"


class StockReceiveHistory(BaseModel):
    """入荷履歴モデル"""
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="店番号")
    staff_code = models.ForeignKey('Staff', on_delete=models.CASCADE, verbose_name="入荷担当")
    received_at = models.DateTimeField(auto_now_add=True, verbose_name="入荷日時")
    products = models.ManyToManyField('Product', through='StockReceiveHistoryItem', related_name='receive_histories')

    class Meta:
        verbose_name = "入荷"
        verbose_name_plural = "入荷履歴一覧"

    def __str__(self):
        return f"{self.store_code} - {self.staff_code} - {self.received_at}"


class StockReceiveHistoryItem(BaseModel):
    """入荷商品履歴モデル(中間テーブル)"""
    history = models.ForeignKey(StockReceiveHistory, related_name='items', on_delete=models.CASCADE)
    jan = models.ForeignKey('Product', on_delete=models.CASCADE, verbose_name="JANコード")
    additional_stock = models.IntegerField(verbose_name="入荷数")

    class Meta:
        verbose_name = "入荷商品"
        verbose_name_plural = "入荷商品一覧"

    def __str__(self):
        return f"{self.jan.jan} - {self.additional_stock}"


class Transaction(BaseModel):
    """取引モデル"""
    class Status(models.TextChoices):
        SALE = "sale", "販売"
        VOID = "void", "取消"
        TRAINING = "training", "トレーニング"
        RETURN = "return", "返品"
        RESALE = "resale", "再売"

    id = models.AutoField(primary_key=True, verbose_name="取引番号")
    relation_return_id = models.ForeignKey('ReturnTransaction', on_delete=models.CASCADE, blank=True, null=True, verbose_name="返品関係id")
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.SALE, verbose_name="取引状態")
    date = models.DateTimeField(verbose_name="購入日時")
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="店番号")
    terminal_id = models.CharField(max_length=50, verbose_name="端末番号")
    staff_code = models.ForeignKey('Staff', to_field='staff_code', on_delete=models.CASCADE, verbose_name="スタッフコード")
    user = models.ForeignKey('CustomUser', on_delete=models.CASCADE, verbose_name="ユーザー")
    total_tax10 = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="10%合計小計")
    total_tax8 = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="8%税額小計")
    tax_amount = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="税額合計")
    total_amount = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="合計購入金額")
    discount_amount = models.IntegerField(verbose_name="値引金額")
    deposit = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="預かり金額")
    change = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="釣銭金額")
    total_quantity = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="合計購入点数")

    class Meta:
        verbose_name = "取引"
        verbose_name_plural = "取引一覧"

    def __str__(self):
        return f"取引番号: {self.id}"


class TransactionDetail(BaseModel):
    """取引商品モデル(中間テーブル)"""
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="sale_products", verbose_name="取引番号")
    jan = models.CharField(max_length=13, verbose_name="JANコード")  # 部門打ちのため外部キー制約を解除
    extra_code = models.CharField(max_length=30, null=True, blank=True, verbose_name="特殊コード")  # POSAなどJANコード以外に必要な番号を記録するフィールド
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(verbose_name="商品価格")
    tax = models.DecimalField(max_digits=3, decimal_places=1, verbose_name="消費税率")
    discount = models.IntegerField(verbose_name="値引金額")
    quantity = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="購入点数")

    class Meta:
        # unique_together = ("transaction", "jan", "tax", "discount")  # 1取引に同じ商品が存在しないことを保証
        constraints = [models.UniqueConstraint(fields=["transaction", "price", "jan", "tax", "discount"], name='TransactionDetail_unique_constraint')]
        verbose_name = "明細"
        verbose_name_plural = "取引詳細一覧"

    def __str__(self):
        return f"{self.transaction} - {self.jan}"


class Payment(BaseModel):
    """支払い方法(中間テーブル)"""
    PAYMENT_METHOD_CHOICES = [
        ("cash", "現金"),
        ("credit", "クレジットカード"),
        ("wallet", "ウォレット"),
        ("voucher", "金券"),
        ("QRcode", "QRコード決済"),
        ("carryover", "引継支払"),
    ]
    transaction = models.ForeignKey('Transaction', related_name='payments', on_delete=models.CASCADE, verbose_name="取引")
    payment_method = models.CharField(max_length=50, choices=PAYMENT_METHOD_CHOICES, verbose_name="支払方法")
    amount = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="支払い金額")

    class Meta:
        unique_together = ("transaction", "payment_method")  # 同じ支払い手段は1度しか使えない
        verbose_name = "支払い"
        verbose_name_plural = "支払い一覧"

    def __str__(self):
        return f"{self.transaction} - {self.payment_method}"


class Wallet(BaseModel):
    user = models.OneToOneField('CustomUser', on_delete=models.CASCADE, related_name='wallet')
    balance = models.IntegerField(verbose_name="残高")

    class Meta:
        verbose_name = "ウォレット"
        verbose_name_plural = "ウォレット一覧"

    def __str__(self):
        return f"Wallet of {self.user.email} - Balance: {self.balance}"

    def deposit(self, amount):
        if amount <= 0:
            raise ValueError("入金額は正の値である必要があります。")

        with transaction.atomic():
            self.balance += amount
            self.save()
            WalletTransaction.objects.create(wallet=self, amount=amount, balance=self.balance, transaction_type='credit')

    def withdraw(self, amount, transaction=None):
        if amount <= 0:
            raise ValueError("出金額は正の値である必要があります。")
        if amount > self.balance:
            raise ValueError("残高が不足しています。")

        self.balance -= amount
        self.save()
        WalletTransaction.objects.create(
            wallet=self,
            amount=amount,
            balance=self.balance,
            transaction_type='debit',
            transaction=transaction
        )


class WalletTransaction(BaseModel):
    TRANSACTION_TYPE_CHOICES = (
        ('credit', '入金'),
        ('debit', '出金'),
        ('refund', '返金'),
    )
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='wallettransactions')
    amount = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="金額")
    balance = models.IntegerField(verbose_name="残高")
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPE_CHOICES, verbose_name="取引タイプ")
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='wallet_transactions', null=True, blank=True, verbose_name="取引")
    return_transaction = models.ForeignKey('ReturnTransaction', on_delete=models.SET_NULL, null=True, blank=True, related_name='wallet_transactions', verbose_name="返品取引")

    class Meta:
        verbose_name = "取引"
        verbose_name_plural = "ウォレット履歴"

    def __str__(self):
        return f"{self.transaction_type.capitalize()} of {self.amount} (残高: {self.balance})"

    def process_refund(self):
        """
        返金の場合は、ウォレット残高に返金額を加算する。
        """
        if self.transaction_type == 'refund':
            self.wallet.balance += self.amount
            self.wallet.save()


class CustomUserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("ユーザー登録にはメールアドレスが必要です")
        
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("スーパーユーザーはスタッフフラグがTrueである必要があります。")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("スーパーユーザーはスーパーユーザーフラグがTrueである必要があります。")

        return self.create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    def generate_ulid():
        return str(ulid.new())

    USER_TYPE_CHOICES = (
        ('staff', 'スタッフ'),
        ('customer', '顧客'),
    )

    id = models.CharField(default=generate_ulid, max_length=26, primary_key=True, editable=False)
    email = models.EmailField(unique=True, verbose_name="メールアドレス")
    password = models.CharField(max_length=128, verbose_name="パスワード")
    user_type = models.CharField(max_length=10, choices=USER_TYPE_CHOICES, verbose_name="ユーザータイプ")
    is_staff = models.BooleanField(default=False, verbose_name="スタッフフラグ")
    is_superuser = models.BooleanField(default=False, verbose_name="スーパーユーザーフラグ")
    is_active = models.BooleanField(default=True, verbose_name="アクティブフラグ")
    last_login = models.DateTimeField(null=True, blank=True, verbose_name="最終ログイン")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="作成日時")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新日時")

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_superuser and self.user_type != 'staff':
            self.user_type = 'staff'
            self.save(update_fields=['user_type'])
            Staff.objects.get_or_create(user=self)

    @property
    def social_user(self):
        return self.social_auth.first()

    class Meta:
        verbose_name = "人"
        verbose_name_plural = "ユーザーマスター"


class Staff(BaseModel):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='staff_profile')
    staff_code = models.CharField(max_length=6, verbose_name="スタッフコード", unique=True)
    name = models.CharField(max_length=30, null=True, blank=True, verbose_name="ユーザー名")
    affiliate_store = models.ForeignKey('Store', null=True, on_delete=models.CASCADE, verbose_name="所属店舗")
    permission = models.ForeignKey("UserPermission", null=True, blank=True, on_delete=models.CASCADE, verbose_name="権限")

    class Meta:
        verbose_name = "人"
        verbose_name_plural = "スタッフマスター"

    def __str__(self):
        return self.staff_code


@receiver(post_save, sender=Staff)
def update_user_group(sender, instance, **kwargs):
    """
    スタッフの役職が更新されたら、対応するDjangoグループにユーザーを自動で割り当てる。
    """
    user = instance.user
    new_permission = instance.permission

    # 一旦すべてのグループからユーザーを削除
    user.groups.clear()

    # 新しい役職に紐づくグループがあれば追加
    if new_permission and new_permission.group:
        user.groups.add(new_permission.group)


class Customer(BaseModel):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='customer_profile')
    name = models.CharField(max_length=30, null=True, blank=True, verbose_name="ユーザー名")

    class Meta:
        verbose_name = "人"
        verbose_name_plural = "顧客マスター"

    def __str__(self):
        return self.name if self.name else f"顧客 ({self.user.email})"


class UserPermission(BaseModel):
    """役職管理モデル"""
    role_name = models.CharField(max_length=20, verbose_name="役職名")
    group = models.ForeignKey(
        Group,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="Django権限グループ",
        help_text="この役職に紐づくDjangoの権限グループを選択してください。"
    )
    register_permission = models.BooleanField(default=True, verbose_name="レジ操作権限")
    void_permission = models.BooleanField(default=False, verbose_name="返品権限")
    stock_receive_permission = models.BooleanField(default=False, verbose_name="入荷権限")
    global_permission = models.BooleanField(default=False, verbose_name="他店舗操作権限")
    change_price_permission = models.BooleanField(default=False, verbose_name="売価変更権限")

    class Meta:
        verbose_name = "役職"
        verbose_name_plural = "役職一覧"

    def __str__(self):
        return self.role_name


class Approval(BaseModel):
    """
    承認番号（8桁の数字）を保存するモデル。
    CustomUser を親とし、外部キーで紐付ける。
    """
    user = models.ForeignKey("CustomUser", on_delete=models.CASCADE, related_name="approvals")
    approval_number = models.CharField(max_length=8)
    is_used = models.BooleanField(default=False, verbose_name="使用済みフラグ")

    class Meta:
        verbose_name = "承認番号"
        verbose_name_plural = "ウォレット承認番号"

    def __str__(self):
        return f"Approval {self.approval_number} for {self.user.email}"


class ReturnTransaction(BaseModel):
    """
    返品取引モデル
    ・元の取引（Transaction）に対する返品情報を保持する。
    ・返品理由、返品日時に加え、返品時の端末ID（terminal_id）や返品担当スタッフコード（staff_code）、返品種別（type）を記録できる。
    """
    RETURN_TYPE_CHOICES = (
        ('all', '全返品'),
        ('partial', '一部返品'),
        ('payment_change', '支払変更')
    )
    modify_id = models.ForeignKey(Transaction, on_delete=models.CASCADE, blank=True, null=True, verbose_name="再売取引")
    return_type = models.CharField(max_length=15, choices=RETURN_TYPE_CHOICES, verbose_name="返品種別")
    origin_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='return_transactions', verbose_name="元取引")
    return_date = models.DateTimeField(auto_now_add=True, verbose_name="返品日時")
    reason = models.TextField(verbose_name="返品理由")
    restock = models.BooleanField(default=True, verbose_name="在庫戻し")
    terminal_id = models.CharField(max_length=50, blank=True, null=True, verbose_name="返品端末番号")
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="返品店番号")
    staff_code = models.ForeignKey(Staff, to_field='staff_code', on_delete=models.CASCADE, verbose_name="返品担当")

    class Meta:
        verbose_name = "返品取引"
        verbose_name_plural = "返品取引一覧"

    def __str__(self):
        return f"返品取引 {self.id}（取消取引：{self.origin_transaction.id}）"


class ReturnDetail(BaseModel):
    """
    返品明細モデル
    ・元の取引明細（TransactionDetail）の内容をそのまま記録する。 
    TransactionDetailと同じ項目を保持することで、返品時に元の取引内容を正確に記録できる。
    """
    return_transaction = models.ForeignKey(ReturnTransaction, on_delete=models.CASCADE, related_name='return_details', verbose_name="返品取引")
    jan = models.CharField(max_length=13, verbose_name="JANコード")  # 部門打ちのため外部キー制約を解除
    extra_code = models.CharField(max_length=30, null=True, blank=True, verbose_name="特殊コード")  # POSAなどJANコード以外に必要な番号を記録するフィールド
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(verbose_name="商品価格")
    tax = models.DecimalField(max_digits=3, decimal_places=1, verbose_name="消費税率")
    discount = models.IntegerField(verbose_name="値引金額")
    quantity = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="購入点数")

    class Meta:
        verbose_name = "返品明細"
        verbose_name_plural = "返品明細一覧"

    def __str__(self):
        return f"{self.return_transaction} - {self.jan}"


class ReturnPayment(BaseModel):
    """
    返金支払モデル
    ・返品時に複数の支払い方法で返金処理を行うための明細レコード。
    ・元の Payment.PAYMENT_METHOD_CHOICES を利用（必要に応じて拡張可）
    """
    REFUND_PAYMENT_METHOD_CHOICES = Payment.PAYMENT_METHOD_CHOICES

    return_transaction = models.ForeignKey(ReturnTransaction, on_delete=models.CASCADE, related_name='return_payments', verbose_name="返品返金支払")
    payment_method = models.CharField(max_length=50, choices=REFUND_PAYMENT_METHOD_CHOICES, verbose_name="返金支払方法")
    amount = models.IntegerField(verbose_name="返金金額")

    class Meta:
        verbose_name = "返金支払"
        verbose_name_plural = "返金支払一覧"

    def __str__(self):
        return f"{self.payment_method} - {self.amount}"


class Department(BaseModel):
    """
    部門管理モデル
    ・分類レベルは "big"（大分類）、"middle"（中分類）、"small"（小分類）から選択する。
    ・大分類の場合、parent は不要（必ず None）。中分類は親として大分類、小分類は親として中分類を選択する必要があり、
    それぞれバリデーションでチェックする。
    ・プロパティ department_code により、階層に応じた連結コード（例："15631"）を返す。
    ・標準消費税率は、"上位部門引継"、"0%"、"8%"、"10%" の中から選択できる。
    ・税率変更フラグ、値引フラグ、部門会計フラグは、「上位部門引継」「許可」「禁止」の３択（デフォルトは「上位部門引継」）となるが、
    大分類の場合は「上位部門引継」を選択できない（選択するとエラー）。
    """
    LEVEL_CHOICES = (
        ('big', '大分類'),
        ('middle', '中分類'),
        ('small', '小分類'),
    )
    DEPART_TAX_CHOICES = (
        ('inherit', '上位部門引継'),
        ('0', '0%'),
        ('8', '8%'),
        ('10', '10%'),
    )
    FLAG_CHOICES = (
        ('inherit', '上位部門引継'),
        ('allow', '許可'),
        ('deny', '禁止'),
    )
    level = models.CharField(max_length=6, choices=LEVEL_CHOICES, verbose_name="分類レベル")
    code = models.CharField(max_length=2, verbose_name="分類コード")
    name = models.CharField(max_length=100, verbose_name="分類名称")
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='sub_departments',
        verbose_name="上位部門"
    )
    tax_rate = models.CharField(
        max_length=7, choices=DEPART_TAX_CHOICES, default='inherit', verbose_name="標準消費税率"
    )
    tax_rate_mod_flag = models.CharField(
        max_length=7, choices=FLAG_CHOICES, default='inherit', verbose_name="税率変更フラグ"
    )
    discount_flag = models.CharField(
        max_length=7, choices=FLAG_CHOICES, default='inherit', verbose_name="値引フラグ"
    )
    accounting_flag = models.CharField(
        max_length=7, choices=FLAG_CHOICES, default='inherit', verbose_name="部門会計フラグ"
    )
    
    class Meta:
        verbose_name = "部門"
        verbose_name_plural = "部門一覧"
        unique_together = (('parent', 'code'),)
    
    def __str__(self):
        if self.level == 'big':
            return f'{self.department_code} - {self.name}'
        elif self.level == 'middle':
            return f'{self.department_code} - {self.name}'
        elif self.level == 'small' and self.parent:
            return f'{self.department_code} - {self.parent.parent.name}_{self.parent.name}_{self.name}'
        return self.name

    def get_department_code(self):
        """連結した部門コードを返す"""
        if self.level == 'big':
            return self.code
        elif self.level == 'middle' and self.parent:
            return f"{self.parent.code}{self.code}"
        elif self.level == 'small' and self.parent and self.parent.parent:
            return f"{self.parent.parent.code}{self.parent.code}{self.code}"
        return self.code

    department_code = property(get_department_code)

    def _get_flag_value(self, flag_field):
        """
        指定されたフラグフィールドの値を取得する。
        上位部門引継が指定されている場合は、親のフラグ値を再帰的に取得する。
        """
        flag_value = getattr(self, flag_field)
        if flag_value == 'inherit' and self.parent:
            return self.parent._get_flag_value(flag_field)  # 親のフラグ値を取得する
        return flag_value

    def get_tax_rate(self):
        """税率を取得する。"""
        return self._get_flag_value('tax_rate')

    def get_tax_rate_mod_flag(self):
        """税率変更フラグを取得する。"""
        return self._get_flag_value('tax_rate_mod_flag')

    def get_discount_flag(self):
        """値引フラグを取得する。"""
        return self._get_flag_value('discount_flag')

    def get_accounting_flag(self):
        """部門会計フラグを取得する。"""
        return self._get_flag_value('accounting_flag')

    def clean(self):
        # 上位部門の必須性と階層チェック
        if self.level in ['middle', 'small'] and not self.parent:
            raise ValidationError("中分類・小分類の場合、上位部門の選択は必須です。")
        if self.level == 'big' and self.parent is not None:
            raise ValidationError("大分類の場合、上位部門は設定できません。")
        if self.level == 'middle':
            if self.parent.level != 'big':
                raise ValidationError("中分類の上位部門は大分類である必要があります。")
        if self.level == 'small':
            if self.parent.level != 'middle':
                raise ValidationError("小分類の上位部門は中分類である必要があります。")
        
        # 大分類の場合、標準仕様の各フラグは「上位部門引継」は選択できない
        if self.level == 'big':
            if self.tax_rate == 'inherit':
                raise ValidationError("大分類の場合、標準消費税率は『上位部門引継』を選択できません。")
            if self.tax_rate_mod_flag == 'inherit':
                raise ValidationError("大分類の場合、税率変更フラグは『上位部門引継』を選択できません。")
            if self.discount_flag == 'inherit':
                raise ValidationError("大分類の場合、値引フラグは『上位部門引継』を選択できません。")
            if self.accounting_flag == 'inherit':
                raise ValidationError("大分類の場合、部門会計フラグは『上位部門引継』を選択できません。")

        super().clean()


def default_expiration_date() -> date:
    # 今日の日付（date）ベースで +730日
    return timezone.localdate() + timedelta(days=730)


class POSA(BaseModel):
    POSA_STATUS = (
        ('created', 'POS未通過'),
        ('salled', 'POS通過'),
        ('charged', 'チャージ済'),
        ('BF_disabled', '無効（販売前）'),
        ('AF_disabled', '無効（販売後）'),
    )
    POSA_TYPE = (
        ('wallet_gift', 'ウォレットギフトカード'),
    )

    # --- ステータス別必須フィールド定義 ---
    # created, BF_disabled の場合は buyer, relative_transaction 不要
    REQUIRE_BUYER_AND_TXN = {status for status, _ in POSA_STATUS}
    REQUIRE_BUYER_AND_TXN -= {'created', 'BF_disabled', 'AF_disabled'}

    # created, BF_disabled, salled の場合は user 不要
    REQUIRE_USER = REQUIRE_BUYER_AND_TXN - {'salled'}
    code = models.CharField(max_length=20, primary_key=True, verbose_name="POSAコード")
    posa_type = models.CharField(max_length=20, choices=POSA_TYPE, verbose_name="POSA種別")
    status = models.CharField(max_length=20, choices=POSA_STATUS, verbose_name="状態")
    is_variable = models.BooleanField(default=False, verbose_name="バリアブルカード")
    card_value = models.IntegerField(validators=[MinValueValidator(0)], null=True, blank=True, verbose_name="カード金額")
    expiration_date = models.DateField(verbose_name="有効期限", default=default_expiration_date)
    buyer = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='posa_buyer', verbose_name="購入者", null=True, blank=True)
    relative_transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='posa_transaction', verbose_name="取引", null=True, blank=True)
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='posa_user', verbose_name="使用者", null=True, blank=True)

    class Meta:
        verbose_name = "POSA"
        verbose_name_plural = "POSA一覧"

    def __str__(self):
        return f"POSAコード: {self.code} - ステータス: {self.status} - 金額: {self.card_value}"

    def clean(self):
        super().clean()

        # 1) 有効期限は date 同士で比較
        today = timezone.localdate()
        if self.expiration_date < today:
            raise ValidationError({"expiration_date": "有効期限は過去の日付に設定できません。"})

        # 2) buyer／relative_transaction の必須チェック
        if self.status in self.REQUIRE_BUYER_AND_TXN:
            if not self.buyer:
                raise ValidationError({"buyer": "このステータスでは購入者の指定が必須です。"})
            if not self.relative_transaction:
                raise ValidationError({"relative_transaction": "このステータスでは取引の指定が必須です。"})

        # 3) user の必須チェック
        if self.status in self.REQUIRE_USER and not self.user:
            raise ValidationError({"user": "このステータスでは使用者の指定が必須です。"})


class BulkGeneratePOSACodes(POSA):
    class Meta:
        proxy = True
        verbose_name = "POSA 一括発行"
        verbose_name_plural = "POSA 一括発行"


class DailySalesReport(Transaction):
    class Meta:
        proxy = True
        verbose_name = "売上参照"
        verbose_name_plural = "日次売上レポート"


class DiscountedJAN(BaseModel):
    """値引きJANモデル"""
    instore_jan = models.CharField(primary_key=True, max_length=13, unique=True, editable=False, verbose_name="インストアJANコード")
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, verbose_name="対象在庫")
    discounted_price = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="値引き後価格")
    is_used = models.BooleanField(default=False, verbose_name="使用済み")

    class Meta:
        verbose_name = "値引きJAN"
        verbose_name_plural = "値引きJAN一覧"

    def __str__(self):
        return self.instore_jan

    def save(self, *args, **kwargs):
        if not self.instore_jan:
            # 'ProductVariation'モデルと'DiscountedJAN'モデルの両方から既存のJANコードを取得
            existing_jans = set(ProductVariation.objects.values_list('instore_jan', flat=True))
            existing_jans.update(DiscountedJAN.objects.values_list('instore_jan', flat=True))
            self.instore_jan = generate_unique_instore_jan(existing_jans)
        super().save(*args, **kwargs)
