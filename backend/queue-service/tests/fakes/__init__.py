"""Test doubles for the other microservices (Event, Reservation, Checkout).

They publish real envelopes to the real exchanges (messages.py builds them), and
FakeReservationService.validate_admission_token checks tokens like Reservation.
"""
