import unittest
from django.utils.timezone import now
from rest_framework.exceptions import ValidationError
from .models import Transaction, TransactionDetail, Payment, Product, Stock, StorePrice
from .serializers import TransactionSerializer

class TransactionSerializerTestCase(unittest.TestCase):
    def setUp(self):
        # サンプルデータを作成
        self.product = Product.objects.create(jan="4993191191007", name="プリンタサーマルラベル", price=2380, tax=10)
        self.store_price = StorePrice.objects.create(store_code="111", jan=self.product, price=900)
        self.stock = Stock.objects.create(store_code="001", jan=self.product, stock=10)

        self.valid_transaction_data = {
            "date": now(),
            "store_code": "111",
            "terminal_id": "T001",
            "staff_code": "1111",
            "sale_products": [
                {"jan": "4993191191007", "quantity": 2, "discount": 100}
            ],
            "payments": [
                {"payment_method": "cash", "amount": 1700}
            ],
        }

    def test_transaction_creation_success(self):
        serializer = TransactionSerializer(data=self.valid_transaction_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        transaction = serializer.save()
        self.assertEqual(Transaction.objects.count(), 1)
        self.assertEqual(TransactionDetail.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        updated_stock = Stock.objects.get(jan=self.product)
        self.assertEqual(updated_stock.stock, 8)  # 在庫が減少していることを確認

    def test_missing_required_fields(self):
        data = self.valid_transaction_data.copy()
        del data["sale_products"]
        serializer = TransactionSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("sale_products", serializer.errors)

    def test_invalid_discount(self):
        data = self.valid_transaction_data.copy()
        data["sale_products"][0]["discount"] = 1200  # 割引が価格を上回る
        serializer = TransactionSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("sale_products", serializer.errors)

    def test_insufficient_payment(self):
        data = self.valid_transaction_data.copy()
        data["payments"][0]["amount"] = 1500  # 支払いが不足
        serializer = TransactionSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("payments", serializer.errors)

    def test_insufficient_stock(self):
        self.stock.stock = 1
        self.stock.save()
        serializer = TransactionSerializer(data=self.valid_transaction_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("sale_products", serializer.errors)

    def test_multiple_payment_methods(self):
        data = self.valid_transaction_data.copy()
        data["payments"] = [
            {"payment_method": "cash", "amount": 1000},
            {"payment_method": "credit", "amount": 700},
        ]
        serializer = TransactionSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_training_mode_no_stock_change(self):
        data = self.valid_transaction_data.copy()
        data["status"] = "training"
        serializer = TransactionSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.assertEqual(Stock.objects.get(jan=self.product).stock, 10)  # 在庫が変化していないことを確認

if __name__ == "__main__":
    unittest.main()
