from django.db import models
from django.db import transaction
from django.contrib.auth.models import BaseUserManager, AbstractBaseUser, PermissionsMixin
from django.core.exceptions import ValidationError


class BaseModel(models.Model):
    """共通フィールドを持つ抽象モデル"""
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="作成日時")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新日時")

    class Meta:
        abstract = True


class Product(BaseModel):
    """商品モデル"""
    class Status(models.TextChoices):
        IN_DEAL = "in_deal", "取引中"
        SPOT = "spot", "スポット"
        DISCON = "discon", "終売"

    TAX_CHOICES = (
        (0, "0% - 免税"),
        (8, "8% - 軽減"),
        (10, "10% - 通常"),
    )

    jan = models.CharField(max_length=13, primary_key=True, verbose_name="JANコード")
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(verbose_name="商品価格")
    tax = models.IntegerField(default=8, choices=TAX_CHOICES, verbose_name="消費税率")
    status = models.CharField(max_length=50, choices=Status.choices, default=Status.IN_DEAL, verbose_name="取引状態")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "商品"
        verbose_name_plural = "商品一覧"

    def clean(self):
        self.validate_jan_code(self.jan)
        self.validate_tax_rate(self.tax)

    def validate_jan_code(self, jan_code):
        """JANコードのチェックディジットを検証する"""
        if len(jan_code) not in [8, 13] or not jan_code.isdigit():
            raise ValidationError("JANコードは8桁または13桁の数字である必要があります。")

        # チェックディジットの計算
        total = sum(int(jan_code[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
        check_digit = (10 - (total % 10)) % 10

        if int(jan_code[-1]) != check_digit:
            raise ValidationError("JANコードのチェックディジットが無効です。")

    def validate_tax_rate(self, tax_rate):
        if tax_rate not in [0, 8, 10]:
            raise ValidationError("税率は0, 8, 10のいずれかでなければなりません。")


    def save(self, *args, **kwargs):
        with transaction.atomic():
            super().save(*args, **kwargs)

            stores = Store.objects.all()
            existing_stocks = set(Stock.objects.filter(jan=self).values_list('store_code_id', flat=True))  # 既存の在庫の店舗コードをセットで取得

            stock_entries = []
            for store in stores:
                if store.store_code not in existing_stocks:  # 既存の在庫がない場合のみ追加
                    stock_entries.append(Stock(store_code=store, jan=self, stock=0))  # 初期在庫は0

            # バッチ処理
            batch_size = 500
            for i in range(0, len(stock_entries), batch_size):
                Stock.objects.bulk_create(stock_entries[i:i + batch_size])


class Store(BaseModel):
    """店舗モデル"""
    store_code = models.CharField(max_length=20, primary_key=True, verbose_name="店番号")
    name = models.CharField(max_length=255, verbose_name="店名")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "店舗"
        verbose_name_plural = "店舗一覧"

    def save(self, *args, **kwargs):
        """新しい店舗が追加された際に全ての商品に対する在庫を生成"""
        super().save(*args, **kwargs)

        # すべての商品に対する在庫をバッチ処理で生成
        products = Product.objects.all()
        stock_entries = []

        for product in products:
            stock_entries.append(Stock(store_code=self, jan=product, stock=0))

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
        unique_together = ("store_code", "jan")  # 店番号とjanコードの組み合わせが一意である
        verbose_name = "在庫"
        verbose_name_plural = "在庫一覧"


class StorePrice(models.Model):
    """店舗ごとの価格モデル"""
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="店番号")
    jan = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="JANコード")
    price = models.IntegerField(verbose_name="店舗価格")

    class Meta:
        unique_together = ("store_code", "jan")  # 店番号とjanコードの組み合わせが一意である
        verbose_name = "店舗価格"
        verbose_name_plural = "店舗価格一覧"

    def __str__(self):
        return f"{self.store_code} - {self.jan} - {self.price}"

    def clean(self):
        """価格が商品価格と同じの場合はValidationErrorを発生させる"""
        if self.price == self.jan.price:
            raise ValidationError("店舗価格は商品価格と異なる必要があります。")

    def save(self, *args, **kwargs):
        self.clean()  # 保存前にcleanメソッドを呼び出す
        super().save(*args, **kwargs)

    def get_price(self):
        """店舗ごとの価格を取得する。価格設定がない場合は商品価格を参照する。"""
        if self.price is not None:
            return self.price
        return self.jan.price  # StorePriceに価格が設定されていない場合はProductから価格を取得


class StockReceiveHistory(models.Model):
    """入荷履歴モデル"""
    store_code = models.ForeignKey(Store, on_delete=models.CASCADE, verbose_name="店番号")
    staff_code = models.ForeignKey("CustomUser", on_delete=models.CASCADE, verbose_name="入荷担当者")
    received_at = models.DateTimeField(auto_now_add=True, verbose_name="入荷日時")
    products = models.ManyToManyField('Product', through='StockReceiveHistoryItem', related_name='receive_histories')

    def __str__(self):
        return f"{self.store_code} - {self.staff_code} - {self.received_at}"

    class Meta:
        verbose_name = "入荷"
        verbose_name_plural = "入荷履歴一覧"


class StockReceiveHistoryItem(models.Model):
    """入荷商品履歴モデル(中間テーブル)"""
    history = models.ForeignKey(StockReceiveHistory, related_name='items', on_delete=models.CASCADE)
    jan = models.ForeignKey('Product', on_delete=models.CASCADE, verbose_name="JANコード")
    additional_stock = models.IntegerField(verbose_name="入荷数")

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
    staff_code = models.ForeignKey("CustomUser", on_delete=models.CASCADE, verbose_name="スタッフコード")
    total_tax10 = models.IntegerField(verbose_name="10%合計小計")
    total_tax8 = models.IntegerField(verbose_name="8%税額小計")
    tax_amount = models.IntegerField(verbose_name="税額合計")
    total_amount = models.IntegerField(verbose_name="合計購入金額")
    discount_amount = models.IntegerField(verbose_name="値引金額")
    deposit = models.IntegerField(verbose_name="預かり金額")
    change = models.IntegerField(verbose_name="釣銭金額")
    total_quantity = models.IntegerField(verbose_name="合計購入点数")

    def __str__(self):
        return f"取引番号: {self.id}"

    class Meta:
        verbose_name = "取引"
        verbose_name_plural = "取引一覧"


class TransactionDetail(BaseModel):
    """取引商品モデル(中間テーブル)"""
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name="sale_products", verbose_name="取引番号")
    jan = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="JANコード")
    name = models.CharField(max_length=255, verbose_name="商品名")
    price = models.IntegerField(verbose_name="商品価格")
    tax = models.DecimalField(max_digits=3, decimal_places=1, verbose_name="消費税率")
    discount = models.IntegerField(verbose_name="値引金額")
    quantity = models.IntegerField(verbose_name="購入点数")

    class Meta:
        unique_together = ("transaction", "jan")  # 1取引に同じ商品が存在しないことを保証
        verbose_name = "明細"
        verbose_name_plural = "取引詳細一覧"


class Payment(models.Model):
    """支払い方法(中間テーブル)"""
    PAYMENT_METHOD_CHOICES = [
        ("cash", "現金"),
        ("credit", "クレジットカード"),
        ("point", "ポイント"),
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


class CustomUserManager(BaseUserManager):
    def create_user(self, staff_code, password=None, **extra_fields):
        """staff_codeとパスワードを使用してユーザーを作成"""
        if not staff_code:
            raise ValueError("ユーザー登録にはスタッフコードが必要です")
        user = self.model(staff_code=staff_code, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, staff_code, password=None, **extra_fields):
        """staff_codeとパスワードを使用してスーパーユーザーを作成"""
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("スーパーユーザーはスタッフフラグがTrueである必要があります。")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("スーパーユーザーはsuフラグがTrueである必要があります。")

        return self.create_user(staff_code, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    staff_code = models.CharField(max_length=6, primary_key=True, verbose_name="スタッフコード")
    name = models.CharField(max_length=30, null=True, blank=True, verbose_name="ユーザー名")
    affiliate_store = models.ForeignKey(Store, null=True, on_delete=models.CASCADE, verbose_name="所属店舗")
    is_staff = models.BooleanField(default=False, verbose_name="スタッフフラグ")
    is_superuser = models.BooleanField(default=False, verbose_name="スーパーユーザーフラグ")
    is_active = models.BooleanField(default=True, verbose_name="アクティブフラグ")
    permission = models.ForeignKey("UserPermission", null=True, blank=True, on_delete=models.CASCADE, verbose_name="権限")
    last_login = models.DateTimeField(null=True, blank=True, verbose_name="最終ログイン")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="作成日時")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新日時")

    objects = CustomUserManager()

    USERNAME_FIELD = "staff_code"
    REQUIRED_FIELDS = []

    def __str__(self):
        return str(self.staff_code)

    class Meta:
        verbose_name = "販売員"
        verbose_name_plural = "販売員一覧"


class UserPermission(BaseModel):
    """役職管理モデル"""
    role_name = models.CharField(max_length=20, verbose_name="役職名")
    register_permission = models.BooleanField(default=True, verbose_name="レジ操作権限")
    void_permission = models.BooleanField(default=False, verbose_name="返品権限")
    stock_receive_permission = models.BooleanField(default=False, verbose_name="入荷権限")
    global_permission = models.BooleanField(default=False, verbose_name="他店舗操作権限")
    change_price_permission = models.BooleanField(default=False, verbose_name="売価変更権限")

    def __str__(self):
        return str(self.role_name)

    class Meta:
        verbose_name = "役職"
        verbose_name_plural = "役職一覧"
