from qaura.core.inputs import CATEGORY_ORDER, diverse_slice, values_for_role


def test_diverse_slice_samples_each_category_before_repeating():
    # This is the exact bug found by running against the buggy_app fixture: the naive
    # `values_for_role(role)[:4]` took the first 4 of a concatenated list, which are
    # all "boundary" category — diverse_slice must not repeat that mistake.
    slice_ = diverse_slice("textbox", 4)
    categories = {v.category for v in slice_}
    assert categories == set(CATEGORY_ORDER)


def test_diverse_slice_respects_requested_size():
    assert len(diverse_slice("textbox", 2)) == 2
    assert len(diverse_slice("textbox", 4)) == 4
    assert len(diverse_slice("textbox", 100)) == len(values_for_role("textbox"))


def test_diverse_slice_first_encoding_value_is_unicode_emoji():
    # Locks in the specific fixture dependency: buggy_app's validate-email bug is
    # triggered by whatever the diverse slice picks first from "encoding" — if this
    # ever changes, the fixture's crash trigger needs to change with it.
    slice_ = diverse_slice("textbox", 4)
    encoding_values = [v for v in slice_ if v.category == "encoding"]
    assert len(encoding_values) == 1
    assert encoding_values[0].label == "unicode_emoji"


def test_diverse_slice_empty_for_non_text_role():
    assert diverse_slice("button", 4) == []


def test_spinbutton_values_are_all_numeric_parseable():
    # Real bug found live, in two rounds: filling a spinbutton (<input type=number>)
    # with non-numeric text (email/phone-shaped OR plain letters like BOUNDARY_VALUES'
    # "single_char"/"very_long") made Playwright's fill() throw, surfaced as a false
    # "crash" finding — QAura fuzzing itself into noise, not a real app bug. Every
    # value here must actually be parseable as a number (matching what a real
    # <input type=number> accepts), not just "not obviously textual".
    from qaura.core.inputs import values_for_role

    values = values_for_role("spinbutton")
    labels = {v.label for v in values}
    assert "text_in_email_field" not in labels
    assert "email_missing_domain" not in labels
    assert "phone_with_letters" not in labels
    assert "date_out_of_range" not in labels
    assert "single_char" not in labels  # "a" -- not numeric
    assert "very_long" not in labels    # "A"*10000 -- not numeric
    assert "negative_quantity" in labels
    for v in values:
        stripped = v.value.strip()
        assert stripped == "" or _looks_numeric(stripped), f"{v.label!r} value {v.value!r} isn't numeric-parseable"


def _looks_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False
