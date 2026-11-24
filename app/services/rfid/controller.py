"""
Docstring for app.services.rfid.controller
This module will be used for custom logic.
"""

from smartx_rfid.devices import DeviceManager
from smartx_rfid.utils import TagList
from smartx_rfid.dispatcher import EventDispatcher
from sqlalchemy import func
from app.core import DISPATCHER_PATH, EXAMPLES_DISPATCHER_PATH
from .integration import Integration
import asyncio
from app.core import settings
import logging
from datetime import datetime, timezone
from app.services.license import license_manager
from smartx_rfid.schemas.tag import WriteTagValidator
from smartx_rfid.clients.renner import get_renner_sku
from app.models.rfid import Inventory, Tag


class Controller:
	def __init__(self, devices: DeviceManager, tags: TagList, integration: Integration):
		self.tags = tags
		self.devices = devices
		self.integration = integration
		self.dispatcher = EventDispatcher(
			dispatches_path=DISPATCHER_PATH,
			example_path=EXAMPLES_DISPATCHER_PATH,
		)
		self.write_list: dict = {}
		if not self.integration.db_manager:
			raise ValueError('Integration must have a db_manager')

		self.current_aisle = 'C131'
		self.current_comparison = None

	# [ EVENTS ]
	def on_event(self, name: str, event_type: str, event_data):
		logging.info(f'[ EVENT ] {name} - {event_type}: {event_data}')
		# if not license_manager.validate_license():
		# 	return
		# asyncio.create_task(
		# 	self.integration.on_event_integration(
		# 		name=name, event_type=event_type, event_data=event_data
		# 	)
		# )
		# asyncio.create_task(
		# 	self.dispatcher.add_async(name=name, event_type=event_type, data=event_data)
		# )

	# [ Reading Events ]
	def on_start(self, name: str):
		logging.info(f'[ START ] {name}')
		# if not license_manager.validate_license():
		# 	return
		if settings.CLEAR_ON_START:
			self.tags.remove_tags_by_device(device=name)

	def on_stop(self, name: str):
		logging.info(f'[ STOP ] {name}')

	# [ Tag Events ]
	def on_new_tag(self, name: str, tag: dict):
		if not self.current_aisle:
			return
		logging.info(f'[ TAG ] {name} - {tag}')
		# if not license_manager.validate_license():
		# 	return

		tag['corredor'] = self.current_aisle
		tag['sku'] = get_renner_sku(tag.get('epc'))
		asyncio.create_task(self.integration.on_tag_integration(tag=tag))
		# asyncio.create_task(self.dispatcher.add_async(name=name, event_type='tag', data=tag))

	def on_existing_tag(self, name: str, tag: dict):
		asyncio.create_task(self.check_target(tag))
		# if settings.ALWAYS_SEND:
		# 	# if not license_manager.validate_license():
			# 	return
			# # asyncio.create_task(self.integration.on_tag_integration(tag=tag))
			# asyncio.create_task(self.dispatcher.add_async(name=name, event_type='tag', data=tag))

	# [ WRITE LIST ]
	def create_write_list_prefix(self, epcs: list, prefix: str):
		self.clear_write_list()
		for epc in epcs:
			target = f'{prefix}{epc[len(prefix):]}'
			current_tag = self.tags.get_by_identifier(epc)
			if current_tag:
				self.add_to_write_list(current_tag, target)
			else:
				logging.error(f'Epc: {epc} not in tags, skipping...')
		return self.write_list

	def add_to_write_list(self, tag: dict, target: str):
		self.write_list[tag.get('tid')] = {
			'target': target.lower(),
			'original_epc': tag.get('epc'),
		}
		tag['target'] = target
		logging.info(f"Added tag {tag.get('tid')} to write list with target {tag.get('target')}")
		self.on_event(name='write_list', event_type='add_to_write_list', event_data=tag)

	def remove_from_write_list(self, tag: dict):
		tid = tag.get('tid')
		if tid in self.write_list:
			logging.info(f'Removed tag {tid} from write list')
			self.on_event(
				name='write_list',
				event_type='remove_from_write_list',
				event_data={**tag, 'original_epc': self.write_list[tid]['original_epc']},
			)
			del self.write_list[tid]
			if not self.write_list:
				logging.info('Write list is now empty')
				self.on_event(name='write_list', event_type='write_list_empty', event_data={})
				return True
		return False

	def clear_write_list(self):
		self.write_list.clear()
		logging.info('Cleared write list')
		self.on_event(name='write_list', event_type='write_list_cleared', event_data={})

	async def check_target(self, tag: dict):
		target = tag.get('target')
		if not target:
			return
		if tag.get('epc') == tag.get('target'):
			logging.info(f"Tag {tag.get('tid')} already has target EPC, removing from write list")
			tag['target'] = None
			self.remove_from_write_list(tag)
			return
		await self.devices.write_epc(
			device_name=tag.get('device'),
			write_tag=WriteTagValidator(
				target_identifier='tid',
				target_value=tag.get('tid'),
				new_epc=tag.get('target'),
				password='00000000',
			),
		)

	def get_match(self, expected: int, found: int):
		if expected == found:
			return '='
		elif expected == 0:
			return '!'
		elif found < expected:
			return '<'
		else:
			return '>'

	@staticmethod
	def _parse_tag_timestamp(value):
		if isinstance(value, datetime):
			timestamp = value
		elif isinstance(value, str):
			try:
				timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
			except ValueError:
				return None
		else:
			return None

		if timestamp.tzinfo is None:
			timestamp = timestamp.replace(tzinfo=timezone.utc)
		return timestamp.astimezone(timezone.utc)

	def generate_comparison(self):
		with self.integration.db_manager.get_session() as session:
			inventory_sku_count = [item.to_dict() for item in session.query(Inventory).all()]
			tags_sku_data = (
				session.query(Tag.sku, Tag.corredor, func.count(Tag.id))
				.group_by(Tag.sku, Tag.corredor)
				.all()
			)

		tags_by_sku = {}
		for sku, corredor, count in tags_sku_data:
			if sku not in tags_by_sku:
				tags_by_sku[sku] = {'count': 0, 'corredores': set()}
			tags_by_sku[sku]['count'] += count
			if corredor:
				tags_by_sku[sku]['corredores'].add(str(corredor))

		inventory_by_sku = {}
		for item in inventory_sku_count:
			item = {
				key: value for key, value in item.items() if key != 'id' and not key.endswith('_at')
			}
			sku = item['sku']
			if sku in inventory_by_sku:
				inventory_by_sku[sku]['endereco'].append(f"{item['endereco']}*{item['quantidade']}")
				inventory_by_sku[sku]['quantidade'] += item['quantidade']
			else:
				inventory_by_sku[sku] = {
					**item,
					'endereco': [f"{item['endereco']}*{item['quantidade']}"],
				}

		report = []
		for sku, item in inventory_by_sku.items():
			item['endereco'] = '|'.join(item.pop('endereco'))
			item['encontrado'] = tags_by_sku.get(sku, {}).get('count', 0)
			item['resultado'] = self.get_match(item.get('quantidade'), item.get('encontrado'))
			report.append(item)

		for sku, tag_data in tags_by_sku.items():
			if sku not in inventory_by_sku:
				corredores = '|'.join(sorted(tag_data['corredores']))
				found = tag_data['count']
				report.append(
					{
						'corredor': corredores,
						'endereco': 'Divergente',
						'sku': sku,
						'quantidade': 0,
						'encontrado': found,
						'resultado': self.get_match(0, found),
					}
				)
		return report

	def set_current_comparison(self, aisle: str):
		aisle = str(aisle).strip()
		logging.info(f'Setting current comparison to aisle: {aisle}')
		candidates = [aisle]
		if aisle.upper().startswith('C'):
			candidates.append(aisle[1:])
		elif aisle.isdigit():
			candidates.append(f'C{aisle}')

		result = []
		for cand in candidates:
			res = self.integration.db_manager.get_where(Inventory, {'corredor': cand})
			if res:
				result = res
				aisle = cand
				break

		if not result:
			raise ValueError(f'Sem itens encontrados no corredor: {aisle}')
		self.current_comparison = [r.to_dict() for r in result]
		self.current_aisle = aisle
		self.tags.clear()

	def get_comparison(self):
		if self.current_comparison is None and self.current_aisle:
			try:
				self.set_current_comparison(self.current_aisle)
			except Exception:
				pass

		if self.current_comparison is None:
			return []
		result = {}

		# get current skus from tags
		current_skus = {}
		for t in self.tags.get_all():
			sku = t.get('sku')
			timestamp = self._parse_tag_timestamp(t.get('timestamp'))
			current_sku = current_skus.get(sku, {})
			max_timestamp = current_sku.get('max_timestamp')
			current_skus[sku] = {
				'qtd': current_sku.get('qtd', 0) + 1,
				'max_timestamp': max(
					(filter(None, (max_timestamp, timestamp))),
					default=None,
				),
			}

		# Build comparison result based on current inventory and found tags
		for r in self.current_comparison:
			sku = r.get('sku')
			if sku not in result:
				current_sku = current_skus.pop(sku, {})
				result[sku] = {
					'sku': sku,
					'enderecos': [],
					'quantidade': 0,
					'encontrado': current_sku.get('qtd', 0),
					'max_timestamp': current_sku.get('max_timestamp'),
				}
			result[sku]['enderecos'].append(
				{'endereco': r.get('endereco'), 'quantidade': r.get('quantidade', 0)}
			)
			result[sku]['quantidade'] += r.get('quantidade', 0)

		# Add any remaining SKUs from current_skus that were not in inventory
		for sku, current_sku in current_skus.items():
			result[sku] = {
				'sku': sku,
				'enderecos': [],
				'quantidade': 0,
				'encontrado': current_sku.get('qtd', 0),
				'max_timestamp': current_sku.get('max_timestamp'),
			}

		# Calculate 'resultado' for each SKU in the result
		for sku, item in result.items():
			item['resultado'] = self.get_match(item.get('quantidade', 0), item.get('encontrado', 0))

		ordered_result = sorted(
			result.values(),
			key=lambda item: (
				item['resultado'] != '!',
				item['max_timestamp'] or datetime.min.replace(tzinfo=timezone.utc),
			),
			reverse=True,
		)
		for item in ordered_result:
			timestamp = item['max_timestamp']
			item['max_timestamp'] = timestamp.isoformat() if timestamp else None

		return ordered_result
