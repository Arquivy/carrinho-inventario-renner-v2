import csv
import logging
import os
import re
import time
from typing import Dict, List, Optional
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.models.rfid import Inventory
from app.services import rfid_manager
from app.services.rfid.renner_codec import (
	get_renner_sku,
	renner_get_address_by_epc,
	renner_serialize_epc_by_address,
)

router = APIRouter(prefix='/api/v1/carrinho', tags=['Carrinho'])


class CarrinhoSettingsUpdate(BaseModel):
	buffer_size: Optional[int] = None
	time_decay: Optional[int] = None
	mode: Optional[str] = None
	corredor: Optional[str] = None


class SimulateTagPayload(BaseModel):
	count: Optional[int] = 10


def parse_renner_address(addr: str) -> Optional[dict]:
	"""Decodes Renner warehouse address: EN{andar}.C{corredor}.S{secao}.N{nivel}.{sobra}
	N1 = Inferior, N2 = Superior
	Ímpar = Esquerda (Q1, Q2)
	Par = Direita (Q3, Q4)
	"""
	if not addr:
		return None
	m = re.search(r'EN(\d+)[.-]C(\d+)[.-]S(\d+)[.-]N(\d+)', addr, re.IGNORECASE)
	if not m:
		return None

	andar = int(m.group(1))
	corredor_num = m.group(2)
	corredor = f'C{corredor_num}'
	secao_num = int(m.group(3))
	secao = f'S{secao_num:03d}'
	nivel_num = int(m.group(4))
	nivel = f'N{nivel_num}'

	is_impar = (secao_num % 2 != 0)
	if is_impar:
		lado = 'Esquerda (Ímpar)'
		quadrante = 'Q1' if nivel == 'N2' else 'Q2'
		antena = 1 if nivel == 'N2' else 2
	else:
		lado = 'Direita (Par)'
		quadrante = 'Q3' if nivel == 'N2' else 'Q4'
		antena = 3 if nivel == 'N2' else 4

	return {
		'andar': andar,
		'corredor': corredor,
		'secao_num': secao_num,
		'secao': secao,
		'nivel': nivel,
		'lado': lado,
		'quadrante': quadrante,
		'antena': antena,
	}


class CarrinhoInventoryEngine:
	def __init__(self):
		self.buffer_size = 6  # 1 to 8 active addresses per quadrant
		self.time_decay = 5  # seconds
		self.mode = 'realtime'  # 'realtime' or 'batch'
		self.selected_corredor = 'C131'
		self.raw_batch_tags: List[dict] = []
		self.read_progress: Dict[str, int] = {}  # endereco -> tags lidas
		self.address_catalog: Dict[str, dict] = {}  # endereco -> metadata
		
		# DYNAMIC ACTIVATION QUEUES per quadrant (Q1, Q2, Q3, Q4)
		# Addresses only enter when their physical address tag (edc0...) is read!
		self.active_queues: Dict[str, List[dict]] = {
			'Q1': [],
			'Q2': [],
			'Q3': [],
			'Q4': [],
		}
		
		# TEMPORARY PRE-READ BUFFER for SKUs read before their address tag
		# Stores items: {'epc': str, 'sku': str, 'timestamp': float}
		self.pre_read_buffer: List[dict] = []
		
		# Set of EPCs already accounted for to prevent double counting
		self.seen_tag_epcs = set()

		self._init_data()

	def _init_data(self):
		"""Loads addresses and SKUs from DB or fallback CSV."""
		self.address_catalog.clear()
		try:
			db = rfid_manager.controller.integration.db_manager
			db_items = db.get_all(Inventory)
			if db_items:
				for item in db_items:
					parsed = parse_renner_address(item.endereco)
					if not parsed:
						continue
					addr = item.endereco
					qtd = int(item.quantidade or 1)
					if addr not in self.address_catalog:
						self.address_catalog[addr] = {
							'endereco': addr,
							'corredor': parsed['corredor'],
							'secao_num': parsed['secao_num'],
							'secao': parsed['secao'],
							'nivel': parsed['nivel'],
							'lado': parsed['lado'],
							'quadrante': parsed['quadrante'],
							'antena': parsed['antena'],
							'skus_esperados': qtd,
							'skus': {item.sku: qtd},
						}
					else:
						self.address_catalog[addr]['skus_esperados'] += qtd
						self.address_catalog[addr]['skus'][item.sku] = (
							self.address_catalog[addr]['skus'].get(item.sku, 0) + qtd
						)
		except Exception:
			pass

		# If DB is empty, try loading from project sample CSV
		if not self.address_catalog:
			csv_path = 'planilha_exemplo_renner.csv'
			if os.path.exists(csv_path):
				with open(csv_path, encoding='utf-8') as f:
					reader = csv.DictReader(f)
					for row in reader:
						addr = row.get('Endereço', '').strip()
						parsed = parse_renner_address(addr)
						if not parsed:
							continue
						qtd = int(row.get('Qtd Atual', 0) or 1)
						sku_item = row.get('Item', '')
						if addr not in self.address_catalog:
							self.address_catalog[addr] = {
								'endereco': addr,
								'corredor': parsed['corredor'],
								'secao_num': parsed['secao_num'],
								'secao': parsed['secao'],
								'nivel': parsed['nivel'],
								'lado': parsed['lado'],
								'quadrante': parsed['quadrante'],
								'antena': parsed['antena'],
								'skus_esperados': qtd,
								'skus': {sku_item: qtd},
							}
						else:
							self.address_catalog[addr]['skus_esperados'] += qtd
							self.address_catalog[addr]['skus'][sku_item] = (
								self.address_catalog[addr]['skus'].get(sku_item, 0) + qtd
							)

		# Pick first available corridor
		corredores = self.get_available_corredores()
		if corredores and self.selected_corredor not in corredores:
			self.selected_corredor = corredores[0]

	def get_available_corredores(self) -> List[str]:
		corrs = set()
		for item in self.address_catalog.values():
			corrs.add(item['corredor'])
		return sorted(list(corrs))

	def _clean_pre_read_buffer(self):
		"""Discards pre-read SKUs that exceeded the temporal decay window."""
		now = time.time()
		cutoff = now - self.time_decay
		self.pre_read_buffer = [item for item in self.pre_read_buffer if item['timestamp'] >= cutoff]

	def on_tag_read(self, tag: dict):
		"""Processes a live tag read from R700 reader."""
		epc = (tag.get('epc') or '').lower().strip()
		if not epc:
			return

		now = time.time()
		self._clean_pre_read_buffer()

		# In batch mode, accumulate raw tag
		if self.mode == 'batch':
			self.raw_batch_tags.append({'epc': epc, 'timestamp': now, 'ant': tag.get('ant')})
			return

		# 1. PHYSICAL ADDRESS TAG READ (starts with 'edc0')
		if epc.startswith('edc0'):
			addr = renner_get_address_by_epc(epc)
			if not addr or addr not in self.address_catalog:
				return
			
			meta = self.address_catalog[addr]
			# Filter by selected corridor
			if meta['corredor'] != self.selected_corredor:
				return
			
			q_id = meta['quadrante']
			queue = self.active_queues[q_id]

			# Check if already active in queue
			existing_slot = next((s for s in queue if s['endereco'] == addr), None)
			if existing_slot:
				existing_slot['last_seen'] = now
				return

			# Not yet in queue: activate it!
			# If queue is full, evict completed slots first, then oldest beyond decay
			if len(queue) >= self.buffer_size:
				concluidos = [s for s in queue if s['status'] == 'Concluído']
				if concluidos:
					queue.remove(concluidos[0])
				else:
					queue.sort(key=lambda s: s['last_seen'])
					queue.pop(0)

			initial_lidos = self.read_progress.get(addr, 0)
			slot = {
				'endereco': addr,
				'secao': meta['secao'],
				'nivel': meta['nivel'],
				'lado': meta['lado'],
				'antena': meta['antena'],
				'skus_esperados': meta['skus_esperados'],
				'skus_lidos': initial_lidos,
				'progresso': round((initial_lidos / meta['skus_esperados'] * 100), 1) if meta['skus_esperados'] > 0 else 0.0,
				'status': 'Concluído' if initial_lidos >= meta['skus_esperados'] else 'Lendo agora',
				'activated_at': now,
				'last_seen': now,
			}

			# Temporal Buffer Reconciliation:
			# Check pre-read buffer for recent SKUs that belong to this newly activated address
			reconciled_indices = []
			for idx, pre_item in enumerate(self.pre_read_buffer):
				sku = pre_item['sku']
				if sku in meta.get('skus', {}) or meta.get('sku') == sku:
					slot['skus_lidos'] += 1
					self.read_progress[addr] = slot['skus_lidos']
					self.seen_tag_epcs.add(pre_item['epc'])
					reconciled_indices.append(idx)
					if slot['skus_lidos'] >= slot['skus_esperados']:
						slot['status'] = 'Concluído'
						break

			# Remove reconciled items from pre-read buffer
			for idx in reversed(reconciled_indices):
				self.pre_read_buffer.pop(idx)

			slot['progresso'] = round(min(100.0, (slot['skus_lidos'] / slot['skus_esperados'] * 100)), 1) if slot['skus_esperados'] > 0 else 0.0
			queue.append(slot)
			logging.info(f'[CARRINHO] Endereço ativado na fila {q_id}: {addr} (Reconciliadas {len(reconciled_indices)} tags do buffer temporal)')
			return

		# 2. PRODUCT SKU TAG READ
		sku = get_renner_sku(epc)
		if not sku:
			return

		# If already counted, just update last seen
		if epc in self.seen_tag_epcs:
			return

		# Look for an active address in all quadrant queues that expects this SKU
		matched_slot = None
		for q_id, queue in self.active_queues.items():
			for slot in queue:
				addr = slot['endereco']
				meta = self.address_catalog.get(addr, {})
				expected_skus = meta.get('skus', {})
				if sku in expected_skus or meta.get('sku') == sku:
					if slot['skus_lidos'] < slot['skus_esperados']:
						matched_slot = slot
						break
			if matched_slot:
				break

		if matched_slot:
			# Address is currently active in queue -> credit read directly
			matched_slot['skus_lidos'] += 1
			matched_slot['last_seen'] = now
			self.read_progress[matched_slot['endereco']] = matched_slot['skus_lidos']
			self.seen_tag_epcs.add(epc)
			pct = round((matched_slot['skus_lidos'] / matched_slot['skus_esperados'] * 100), 1)
			matched_slot['progresso'] = pct
			if pct >= 100.0:
				matched_slot['status'] = 'Concluído'
			logging.info(f"[CARRINHO] SKU {sku} creditado ao endereço ativo {matched_slot['endereco']} ({matched_slot['skus_lidos']}/{matched_slot['skus_esperados']})")
		else:
			# Not currently active -> push to pre-read buffer to wait for address tag
			self.pre_read_buffer.append({'epc': epc, 'sku': sku, 'timestamp': now})
			logging.info(f'[CARRINHO] SKU {sku} guardado no buffer pré-leitura (Aguardando tag edc0...)')

	def get_state(self) -> dict:
		self._clean_pre_read_buffer()
		corredor = self.selected_corredor
		items_corredor = [
			item for item in self.address_catalog.values() if item['corredor'] == corredor
		]
		items_corredor.sort(key=lambda x: (x['secao_num'], x['endereco']))

		# Calculate quadrant stats based on total catalog vs active queue
		quadrant_stats = {}
		for q_id in ['Q1', 'Q2', 'Q3', 'Q4']:
			q_catalog = [it for it in items_corredor if it['quadrante'] == q_id]
			total_q = len(q_catalog)
			concluidos_q = sum(1 for it in q_catalog if self.read_progress.get(it['endereco'], 0) >= it['skus_esperados'])
			quadrant_stats[q_id] = {
				'total': total_q,
				'concluidos': concluidos_q,
				'restantes': max(0, total_q - concluidos_q),
			}

		total_skus = sum(item['skus_esperados'] for item in items_corredor)
		total_lidos = sum(
			min(self.read_progress.get(item['endereco'], 0), item['skus_esperados'])
			for item in items_corredor
		)
		pct_geral = round((total_lidos / total_skus * 100) if total_skus > 0 else 0.0, 1)

		return {
			'corredor': corredor,
			'corredores_disponiveis': self.get_available_corredores(),
			'buffer_size': self.buffer_size,
			'time_decay': self.time_decay,
			'mode': self.mode,
			'raw_batch_count': len(self.raw_batch_tags),
			'pre_read_count': len(self.pre_read_buffer),
			'quadrants': self.active_queues,
			'quadrant_stats': quadrant_stats,
			'resumo': {
				'total_enderecos': len(items_corredor),
				'total_skus': total_skus,
				'total_lidos': total_lidos,
				'progresso_geral': pct_geral,
			},
		}

	def simulate_read_step(self, count: int = 10):
		"""Simulates dynamic tag reads: activates next addresses and reads SKUs."""
		corredor = self.selected_corredor
		items_corredor = [
			item for item in self.address_catalog.values() if item['corredor'] == corredor
		]
		items_corredor.sort(key=lambda x: (x['secao_num'], x['endereco']))

		# If queues are empty, activate the first unread addresses per quadrant
		for q_id in ['Q1', 'Q2', 'Q3', 'Q4']:
			if len(self.active_queues[q_id]) == 0:
				q_candidates = [
					it for it in items_corredor
					if it['quadrante'] == q_id and self.read_progress.get(it['endereco'], 0) < it['skus_esperados']
				]
				if q_candidates:
					# Simulate address tag read
					addr = q_candidates[0]['endereco']
					try:
						epc_addr = renner_serialize_epc_by_address(addr)
					except Exception:
						epc_addr = f'edc000000000000000000000'
					self.on_tag_read({'epc': epc_addr, 'rssi': -45, 'ant': 1})

		# Now simulate SKU reads on active addresses
		for q_id, slots in self.active_queues.items():
			active_slots = [s for s in slots if s['status'] == 'Lendo agora']
			if not active_slots:
				continue
			target = active_slots[0]
			meta = self.address_catalog.get(target['endereco'], {})
			skus = list(meta.get('skus', {}).keys())
			sku = skus[0] if skus else '100001'
			step = max(3, target['skus_esperados'] // 4)
			for i in range(step):
				serial = int(time.time() * 1000) % 1000000 + i
				epc = f'0000{int(sku):012x}{serial:08x}'
				self.on_tag_read({'epc': epc, 'rssi': -50, 'ant': target['antena']})

	def process_batch(self):
		"""Flushes accumulated raw batch tags into progression."""
		count = len(self.raw_batch_tags)
		self.mode = 'realtime'
		for tag in self.raw_batch_tags:
			self.on_tag_read(tag)
		self.raw_batch_tags.clear()
		return {'message': f'Lote de {count} tags processado com sucesso!'}

	def reset(self):
		self.read_progress.clear()
		self.raw_batch_tags.clear()
		self.pre_read_buffer.clear()
		self.seen_tag_epcs.clear()
		for q in self.active_queues:
			self.active_queues[q].clear()


carrinho_engine = CarrinhoInventoryEngine()


@router.get('/device_status')
async def get_device_status():
	"""Dynamically checks real reader connection status from rfid_manager."""
	try:
		info_list = rfid_manager.devices.get_device_info()
		devices = rfid_manager.devices.get_devices()

		connected_devices = [d for d in info_list if d.get('is_connected')]
		is_reading = any(d.get('is_reading') for d in info_list)

		if not connected_devices:
			return {
				'has_device': len(devices) > 0,
				'connected': False,
				'reading': False,
				'device_name': None,
				'total_configured': len(devices),
				'message': 'Nenhum leitor conectado no momento',
				'devices': info_list,
			}

		primary = connected_devices[0]
		return {
			'has_device': True,
			'connected': True,
			'reading': is_reading,
			'device_name': primary.get('name'),
			'device_type': primary.get('device_type'),
			'antenna_count': primary.get('antennas', 4),
			'total_configured': len(devices),
			'message': f"Leitor {primary.get('name')} conectado",
			'devices': info_list,
		}
	except Exception as e:
		return {
			'has_device': False,
			'connected': False,
			'reading': False,
			'device_name': None,
			'total_configured': 0,
			'error': str(e),
			'devices': [],
		}


@router.get('/state')
async def get_state():
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())


@router.post('/settings')
async def update_settings(payload: CarrinhoSettingsUpdate):
	if payload.buffer_size is not None:
		carrinho_engine.buffer_size = max(1, min(8, payload.buffer_size))
	if payload.time_decay is not None:
		carrinho_engine.time_decay = max(1, min(30, payload.time_decay))
	if payload.mode in ('realtime', 'batch'):
		carrinho_engine.mode = payload.mode
	if payload.corredor:
		carrinho_engine.selected_corredor = payload.corredor
		carrinho_engine.reset()
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())


@router.post('/toggle_inventory')
async def toggle_inventory():
	"""Starts or stops inventory on all connected readers."""
	try:
		info_list = rfid_manager.devices.get_device_info()
		is_reading = any(d.get('is_reading') for d in info_list)
		if is_reading:
			await rfid_manager.devices.stop_inventory_all()
			action = 'stopped'
		else:
			await rfid_manager.devices.start_inventory_all()
			action = 'started'
		return {'status': 'ok', 'action': action}
	except Exception as e:
		return JSONResponse(status_code=500, content={'error': str(e)})


@router.post('/simulate_step')
async def simulate_step(payload: SimulateTagPayload):
	carrinho_engine.simulate_read_step(payload.count or 10)
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())


@router.post('/process_batch')
async def process_batch():
	res = carrinho_engine.process_batch()
	return JSONResponse(status_code=200, content=res)


@router.post('/reset')
async def reset_carrinho():
	carrinho_engine.reset()
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())


@router.post('/reload_database')
async def reload_database():
	carrinho_engine._init_data()
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())
