"""Tests for every function in the business rules engine."""
import pytest
from app.rules.business_rules import (
    check_missing_fields,
    evaluate_capacity,
    evaluate_existing_customer,
    evaluate_lead,
    evaluate_proposal_value,
    REQUIRED_FIELDS,
)


def test_check_missing_fields_all_present():
    collected = {
        "name": "Ramesh Kumar",
        "email": "ramesh@example.com",
        "project_type": "commercial",
        "location": "Kolkata",
        "capacity_kw": 50,
    }
    assert check_missing_fields(collected) == []


def test_check_missing_fields_partial():
    collected = {"name": "Ramesh Kumar", "email": "r@example.com"}
    missing = check_missing_fields(collected)
    assert "project_type" in missing
    assert "location" in missing
    assert "capacity_kw" in missing
    assert "name" not in missing


def test_check_missing_fields_empty():
    missing = check_missing_fields({})
    assert set(missing) == set(REQUIRED_FIELDS)


def test_evaluate_proposal_value_below_threshold():
    assert evaluate_proposal_value(499_999) is False


def test_evaluate_proposal_value_at_threshold():
    assert evaluate_proposal_value(500_000) is False


def test_evaluate_proposal_value_above_threshold():
    assert evaluate_proposal_value(500_001) is True


def test_evaluate_proposal_value_none():
    assert evaluate_proposal_value(None) is False


def test_evaluate_capacity_below_threshold():
    assert evaluate_capacity(999) is False  # 999 kW < 1000 kW (1 MW)


def test_evaluate_capacity_at_threshold():
    assert evaluate_capacity(1000) is False


def test_evaluate_capacity_above_threshold():
    assert evaluate_capacity(1001) is True


def test_evaluate_capacity_none():
    assert evaluate_capacity(None) is False


def test_evaluate_existing_customer_true():
    assert evaluate_existing_customer(True) is True


def test_evaluate_existing_customer_false():
    assert evaluate_existing_customer(False) is False


def test_evaluate_lead_not_ready():
    result = evaluate_lead({"name": "Test"})
    assert result.is_ready_for_proposal is False
    assert len(result.missing_fields) > 0


def test_evaluate_lead_ready_no_signoff():
    collected = {
        "name": "Priya Sen",
        "email": "priya@example.com",
        "project_type": "residential",
        "location": "Howrah",
        "capacity_kw": 5,
    }
    result = evaluate_lead(collected, total_value=200_000)
    assert result.is_ready_for_proposal is True
    assert result.requires_manager_signoff is False
    assert result.missing_fields == []


def test_evaluate_lead_ready_requires_signoff_by_value():
    collected = {
        "name": "Priya Sen",
        "email": "priya@example.com",
        "project_type": "industrial",
        "location": "Howrah",
        "capacity_kw": 200,
    }
    result = evaluate_lead(collected, total_value=600_000)
    assert result.is_ready_for_proposal is True
    assert result.requires_manager_signoff is True


def test_evaluate_lead_ready_requires_signoff_by_capacity():
    collected = {
        "name": "Priya Sen",
        "email": "priya@example.com",
        "project_type": "industrial",
        "location": "Howrah",
        "capacity_kw": "1500",  # string, should be coerced
    }
    result = evaluate_lead(collected, total_value=100_000)
    assert result.is_ready_for_proposal is True
    assert result.requires_manager_signoff is True
    assert result.requires_director_notify is True


def test_lead_score_high():
    collected = {
        "name": "A", "email": "a@b.com", "project_type": "residential",
        "location": "X", "capacity_kw": 10,
        "budget": 500000, "timeline": "3 months", "monthly_electricity_bill": 8000,
    }
    result = evaluate_lead(collected)
    assert result.lead_score == "high"


def test_lead_score_low():
    collected = {"name": "A", "email": "a@b.com", "project_type": "residential", "location": "X", "capacity_kw": 10}
    result = evaluate_lead(collected)
    assert result.lead_score == "low"
