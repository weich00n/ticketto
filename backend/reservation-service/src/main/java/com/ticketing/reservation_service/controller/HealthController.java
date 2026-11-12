package com.ticketing.reservation_service.controller;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

import com.ticketing.reservation_service.dto.HealthView;

/**
 * Liveness probe for the load balancer. No prefix and no authentication. It answers only
 * "this process is serving requests"; a dependency being down is reported per endpoint as
 * 503 DEPENDENCY_UNAVAILABLE, not here.
 */
@RestController
public class HealthController {

	@GetMapping("/health")
	public HealthView health() {
		return new HealthView("UP");
	}
}
