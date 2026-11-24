"""
RFID models for SMARTX Connector.

Defines the Tag and Event models for storing RFID reader data
with proper indexing and relationships.
"""

import re

from sqlalchemy import Column, Index, Integer, String, Text
from sqlalchemy.orm import validates

from smartx_rfid.models import Base, BaseMixin


class Tag(Base, BaseMixin):
	"""
	RFID Tag model for storing tag read events.

	Stores information about RFID tags detected by readers,
	including EPC, TID, antenna, and signal strength data.
	"""

	__tablename__ = 'tags'
	__cleanup__ = True

	# Primary key
	id = Column(Integer, primary_key=True, autoincrement=True)

	# Device identification
	device = Column(String(100), nullable=False)

	# RFID data fields
	epc = Column(String(24), nullable=False, index=True, unique=True)
	sku = Column(String(100), nullable=False, index=True)
	ant = Column(Integer, nullable=False)
	rssi = Column(Integer, nullable=False)
	corredor = Column(String(100), nullable=False, index=True)


class Event(Base, BaseMixin):
	"""
	RFID Event model for storing system and reader events.

	Stores various types of events from RFID readers and the system,
	including errors, status changes, and operational events.
	"""

	__tablename__ = 'events'
	__cleanup__ = True

	# Primary key
	id = Column(Integer, primary_key=True, autoincrement=True)

	# Device identification
	device = Column(String(100), nullable=False, index=True)

	# Event classification
	event_type = Column(String(50), nullable=False, index=True)

	# Event data
	event_data = Column(Text, nullable=False)

	# Indexes for optimal query performance
	__table_args__ = (
		# Composite indexes
		Index('ix_events_device_type', 'device', 'event_type'),
	)


class Inventory(Base, BaseMixin):
	__tablename__ = 'inventory'
	__cleanup__ = True

	# Primary key
	id = Column(Integer, primary_key=True, autoincrement=True)
	corredor = Column(String(100), nullable=False, index=True)
	endereco = Column(String(100), nullable=False, index=True, unique=True)
	sku = Column(String(100), nullable=False, index=True)
	quantidade = Column(Integer, nullable=False)

	@staticmethod
	def corredor_from_endereco(endereco: str) -> str | None:
		if not endereco:
			return None
		match = re.search(r'C([^.]+)\.', endereco)
		if match:
			return match.group(1)
		match = re.search(r'C(\d+)', endereco, re.IGNORECASE)
		return match.group(1) if match else None

	@validates('endereco')
	def set_corredor_from_endereco(self, key, endereco):
		corredor = self.corredor_from_endereco(endereco)
		if corredor is not None:
			self.corredor = corredor
		return endereco
