package com.ticketing.reservation_service.repository;

import org.springframework.data.jpa.repository.JpaRepository;

import com.ticketing.reservation_service.model.EventSaleStatus;

public interface EventSaleStatusRepository extends JpaRepository<EventSaleStatus, String> {
}
