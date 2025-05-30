import random, string
from django.utils import timezone
from datetime import timedelta
from django.apps import apps


def calculate_checksum(numbers):
    """チェックディジットを計算するための関数"""
    accumulated_sum, multiplier = 0, 3
    for number in reversed(numbers):
        accumulated_sum += int(number) * multiplier
        multiplier = 1 if multiplier == 3 else 3
    return accumulated_sum


def is_valid_jan_code(jan_code_str):
    """JANコードのチェックディジットを検証する"""
    if len(jan_code_str) not in [8, 13] or not jan_code_str.isdigit():
        return False  # JANコードは8桁または13桁の数字である必要があります

    numbers = list(jan_code_str[:-1])  # 最後の桁を除く
    expected_cd = calculate_checksum(numbers) % 10
    expected_cd = 10 - expected_cd if expected_cd != 0 else 0

    actual_cd = int(jan_code_str[-1])  # 最後の桁を取得
    return expected_cd == actual_cd


def calculate_check_digit(base_jan):
    """JANコードのチェックデジットを計算する"""
    if len(base_jan) != 12 or not base_jan.isdigit():
        raise ValueError("JANコードは12桁の数字である必要があります。")

    total = sum(int(base_jan[i]) * (1 if i % 2 == 0 else 3) for i in range(12))
    check_digit = (10 - (total % 10)) % 10
    return check_digit


def generate_unique_instore_jan(existing_variations):
    """ユニークなインストアJANを生成する関数"""
    while True:
        random_code = "20" + ''.join(random.choices('0123456789', k=10))
        check_digit = calculate_check_digit(random_code)
        instore_jan = random_code + str(check_digit)
        if instore_jan not in existing_variations:
            return instore_jan


def generate_random_password(length):
    """指定された長さのランダムなパスワードを生成します。"""
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(characters) for _ in range(length))


def generate_unique_posa_code():
    POSA = apps.get_model('api_v1', 'POSA')
    while True:
        # 先頭8桁を99902000に設定し、残りの12桁をランダムに生成
        code = '99902000' + str(random.randint(0, 10**12 - 1)).zfill(12)
        if not POSA.objects.filter(code=code).exists():
            return code


def bulk_generate_posa_codes(posa_type, is_variable, card_value, quantity):
    POSA = apps.get_model('api_v1', 'POSA')
    expiration = (timezone.now() + timedelta(days=730)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    for _ in range(quantity):
        code = generate_unique_posa_code()
        POSA.objects.create(
            code=code,
            posa_type=posa_type,
            status='created',
            is_variable=is_variable,
            card_value=None if is_variable else card_value,
            expiration_date=expiration,
        )