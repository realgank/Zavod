import unittest
from decimal import Decimal

from database import parse_decimal, RecipeComponent
from zavod.recipes import parse_recipe_table


class ParseDecimalTests(unittest.TestCase):
    def test_parse_decimal_with_spaces(self) -> None:
        self.assertEqual(parse_decimal("1 234 567"), Decimal("1234567"))

    def test_parse_decimal_with_narrow_no_break_space_and_comma(self) -> None:
        value = "1\u202f234\u202f567,89"
        self.assertEqual(parse_decimal(value), Decimal("1234567.89"))

    def test_parse_decimal_with_mixed_separators(self) -> None:
        self.assertEqual(parse_decimal("1,234.5"), Decimal("1234.5"))
        self.assertEqual(parse_decimal("1.234,5"), Decimal("1234.5"))


class ParseRecipeTableTests(unittest.TestCase):
    def test_parse_recipe_table_with_inline_entries_and_spaces(self) -> None:
        table = (
            "1001 Tritanium 1 200 2 400 1002 Pyerite 3 000 15 000"
        )
        components = parse_recipe_table(table)
        self.assertEqual(len(components), 2)
        self.assertEqual(components[0], RecipeComponent("Tritanium", Decimal("1200"), Decimal("2")))
        self.assertEqual(components[1], RecipeComponent("Pyerite", Decimal("3000"), Decimal("5")))

    def test_parse_recipe_table_with_multiline_spacing(self) -> None:
        table = """
            1001    Tritanium    1 200    2 400
            1002    Pyerite      3 000    15 000
        """
        components = parse_recipe_table(table)
        self.assertEqual(len(components), 2)
        self.assertEqual(components[0], RecipeComponent("Tritanium", Decimal("1200"), Decimal("2")))
        self.assertEqual(components[1], RecipeComponent("Pyerite", Decimal("3000"), Decimal("5")))


if __name__ == "__main__":
    unittest.main()
