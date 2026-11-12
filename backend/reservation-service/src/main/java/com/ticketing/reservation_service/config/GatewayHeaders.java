package com.ticketing.reservation_service.config;

/**
 * The headers API Gateway sets from the verified Cognito access token.
 *
 * <p>This service does not verify access tokens: the gateway's JWT authorizer does, and its
 * parameter mapping overwrites whatever the client sent, so these headers cannot be forged by a
 * browser. That is only true while the service is unreachable except through the gateway, which
 * the security groups must enforce.
 */
public final class GatewayHeaders {

	/** The buyer's permanent Cognito id. This is the authenticated identity. */
	public static final String USER_SUB = "X-User-Sub";

	public static final String USER_USERNAME = "X-User-Username";

	/** Arrives as a string such as "[organiser]", not a JSON array. */
	public static final String USER_GROUPS = "X-User-Groups";

	private GatewayHeaders() {
	}
}
