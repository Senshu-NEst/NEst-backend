import random
import ulid
from django.db import models, transaction
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin
from django.core.exceptions import ValidationError
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


class Product(BaseModel):
    """商品モデル"""
    class Status(models.TextChoices):
        IN_DEAL = "in_deal", "取引中"
        SPOT = "spot", "スポット"
        DISCON = "discon", "終売"

    TAX_CHOICES = (
        (0.0, "0% - 免税"),
        (8.0, "8% - 軽減"),
        (10.0, "10% - 通常"),
    )

    jan = models.CharField(max_length=13, primary_key=True, verbose_name="JANコード")
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="商品価格")
    tax = models.DecimalField(max_digits=3, decimal_places=1, default=8.0, choices=TAX_CHOICES, verbose_name="消費税率")
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.IN_DEAL, verbose_name="取引状態")
    disable_change_tax = models.BooleanField(default=False, verbose_name="POSでの税率変更を禁止")
    disable_change_price = models.BooleanField(default=False, verbose_name="POSでの価格変更を禁止")

    class Meta:
        verbose_name = "商品"
        verbose_name_plural = "商品一覧"

    def __str__(self):
        return self.jan

    def clean(self):
        is_valid_jan_code(self.jan)
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
        return self.name

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
        VOID = "void", "返品"
        TRAINING = "training", "トレーニング"

    id = models.AutoField(primary_key=True, verbose_name="取引番号")
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
    jan = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="JANコード")
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(verbose_name="商品価格")
    tax = models.DecimalField(max_digits=3, decimal_places=1, verbose_name="消費税率")
    discount = models.IntegerField(verbose_name="値引金額")
    quantity = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="購入点数")

    class Meta:
        unique_together = ("transaction", "jan")  # 1取引に同じ商品が存在しないことを保証
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
    )
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='wallettransactions')
    amount = models.IntegerField(validators=[MinValueValidator(0)], verbose_name="金額")
    balance = models.IntegerField(verbose_name="残高")
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPE_CHOICES, verbose_name="取引タイプ")
    transaction = models.ForeignKey('Transaction', on_delete=models.CASCADE, related_name='wallet_transactions', null=True, blank=True)

    class Meta:
        verbose_name = "取引"
        verbose_name_plural = "ウォレット履歴"

    def __str__(self):
        return f"{self.transaction_type.capitalize()} of {self.amount} in wallet of {self.wallet.user.email} on {self.created_at}"


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
