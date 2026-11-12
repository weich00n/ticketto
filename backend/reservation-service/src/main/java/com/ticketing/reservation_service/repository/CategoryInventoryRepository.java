package com.ticketing.reservation_service.repository;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;
import org.springframework.transaction.annotation.Transactional;

import jakarta.persistence.LockModeType;

import com.ticketing.reservation_service.model.CategoryInventory;
import com.ticketing.reservation_service.model.CategoryInventoryId;

public interface CategoryInventoryRepository extends JpaRepository<CategoryInventory, CategoryInventoryId> {

	List<CategoryInventory> findByEventIdOrderByCategoryId(String eventId);

	/**
	 * The event's categories, locked for the rest of the caller's transaction and read as last
	 * committed. Always in category order, so every caller takes the locks the same way round.
	 */
	@Lock(LockModeType.PESSIMISTIC_WRITE)
	@Query("select c from CategoryInventory c where c.eventId = :eventId order by c.categoryId")
	List<CategoryInventory> lockByEventId(@Param("eventId") String eventId);

	/**
	 * Moves tickets from held to sold. The addition is done by the database rather than read
	 * into Java and written back, so two confirmations landing together cannot lose a count.
	 */
	@Transactional
	@Modifying(clearAutomatically = true)
	@Query("update CategoryInventory c set c.soldQuantity = c.soldQuantity + :quantity "
			+ "where c.eventId = :eventId and c.categoryId = :categoryId")
	int addSold(@Param("eventId") String eventId, @Param("categoryId") String categoryId,
			@Param("quantity") int quantity);
}
