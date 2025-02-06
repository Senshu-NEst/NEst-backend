import random, string

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

def generate_random_password(length):
    """指定された長さのランダムなパスワードを生成します。"""
    characters = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(characters) for _ in range(length))
