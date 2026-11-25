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
		# Keyed by EPC to prevent duplicates: epc -> {'sku': str, 'timestamp': float}
		self.pre_read_buffer: Dict[str, dict] = {}
		self._last_buffer_clean: float = 0.0
		
		# Set of EPCs already accounted for to prevent double counting
		self.seen_tag_epcs = set()

		# Cross-corridor leakage tracking & spatial reconciliation
		# epc_to_address: epc -> endereco where it was normally read
		self.epc_to_address: Dict[str, str] = {}
		# leakage_records: epc -> {'epc', 'sku', 'corredor_leitura', 'endereco_vazamento', 'antena', 'rssi', 'timestamp'}
		self.leakage_records: Dict[str, dict] = {}
		# recovered_by_leakage: target_endereco -> list of recovered tag details
		self.recovered_by_leakage: Dict[str, List[dict]] = {}
		self.last_reconcile_summary: Optional[dict] = None
		self.leakage_risk_cache: Dict[str, dict] = {}

		self._init_data()

	def _init_data(self):
		"""Loads addresses and SKUs from DB or fallback CSV."""
		self.address_catalog.clear()
		self.leakage_risk_cache.clear()
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

	def _clean_pre_read_buffer(self, force: bool = False):
		"""Discards pre-read SKUs that exceeded the temporal decay window (throttled)."""
		now = time.time()
		if not force and (now - self._last_buffer_clean) < 0.5:
			return
		self._last_buffer_clean = now
		if not self.pre_read_buffer:
			return
		cutoff = now - self.time_decay
		expired = [epc for epc, item in self.pre_read_buffer.items() if item['timestamp'] < cutoff]
		for epc in expired:
			del self.pre_read_buffer[epc]

	def _find_nearest_active_address(self, tag_ant: Optional[int] = None) -> Optional[str]:
		"""Finds the closest active address in the current corridor (preferring same antenna)."""
		same_ant_slots = []
		all_slots = []
		for queue in self.active_queues.values():
			for slot in queue:
				all_slots.append(slot)
				if tag_ant is not None and slot.get('antena') == tag_ant:
					same_ant_slots.append(slot)

		candidates = same_ant_slots if same_ant_slots else all_slots
		if not candidates:
			return None
		best = max(candidates, key=lambda s: s.get('last_seen', 0.0))
		return best['endereco']

	def _record_leakage_candidate(self, epc: str, sku: str, tag: dict, now: float):
		"""Stores/updates a lightweight leakage candidate anchored to the nearest active address."""
		tag_ant = tag.get('ant')
		anchor_addr = self._find_nearest_active_address(tag_ant)
		if not anchor_addr:
			return

		rssi = tag.get('rssi')
		existing = self.leakage_records.get(epc)
		# Keep the reading with strongest RSSI (least negative)
		if existing is None or (rssi is not None and (existing.get('rssi') is None or rssi > existing['rssi'])):
			self.leakage_records[epc] = {
				'epc': epc,
				'sku': sku,
				'corredor_leitura': self.selected_corredor,
				'endereco_vazamento': anchor_addr,
				'antena': tag_ant,
				'rssi': rssi,
				'timestamp': now,
			}

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
			risk_info = self._get_address_leakage_risk(self.selected_corredor, addr)
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
				'risco_vazamento': risk_info['risco_vazamento'],
				'corredores_risco': risk_info['corredores_risco'],
				'skus_em_risco_count': len(risk_info['skus_em_risco']),
				'activated_at': now,
				'last_seen': now,
			}

			# Temporal Buffer Reconciliation:
			# Check pre-read buffer for recent SKUs that belong to this newly activated address
			self._clean_pre_read_buffer(force=True)
			reconciled_epcs = []
			for pre_epc, pre_item in self.pre_read_buffer.items():
				sku = pre_item['sku']
				if sku in meta.get('skus', {}) or meta.get('sku') == sku:
					slot['skus_lidos'] += 1
					self.read_progress[addr] = slot['skus_lidos']
					self.seen_tag_epcs.add(pre_epc)
					self.epc_to_address[pre_epc] = addr
					reconciled_epcs.append(pre_epc)
					if slot['skus_lidos'] >= slot['skus_esperados']:
						slot['status'] = 'Concluído'
						break

			# Remove reconciled items from pre-read buffer
			for pre_epc in reconciled_epcs:
				del self.pre_read_buffer[pre_epc]

			slot['progresso'] = round(min(100.0, (slot['skus_lidos'] / slot['skus_esperados'] * 100)), 1) if slot['skus_esperados'] > 0 else 0.0
			queue.append(slot)
			logging.info(f'[CARRINHO] Endereço ativado na fila {q_id}: {addr} (Reconciliadas {len(reconciled_epcs)} tags do buffer temporal)')
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
		excess_slot = None
		for _q_id, queue in self.active_queues.items():
			for slot in queue:
				addr = slot['endereco']
				meta = self.address_catalog.get(addr, {})
				expected_skus = meta.get('skus', {})
				if sku in expected_skus or meta.get('sku') == sku:
					if slot['skus_lidos'] < slot['skus_esperados']:
						matched_slot = slot
						break
					elif excess_slot is None:
						excess_slot = slot
			if matched_slot:
				break

		target_slot = matched_slot or excess_slot
		if target_slot:
			# Address is currently active in queue -> credit read directly (including excess)
			target_slot['skus_lidos'] += 1
			target_slot['last_seen'] = now
			self.read_progress[target_slot['endereco']] = target_slot['skus_lidos']
			self.seen_tag_epcs.add(epc)
			self.epc_to_address[epc] = target_slot['endereco']
			pct = round((target_slot['skus_lidos'] / target_slot['skus_esperados'] * 100), 1) if target_slot['skus_esperados'] > 0 else 0.0
			target_slot['progresso'] = pct
			if target_slot['skus_lidos'] > target_slot['skus_esperados']:
				target_slot['status'] = 'Excesso'
			elif pct >= 100.0:
				target_slot['status'] = 'Concluído'
		else:
			# Not currently active -> store/refresh in pre-read buffer & record leakage anchor
			self.pre_read_buffer[epc] = {'sku': sku, 'timestamp': now}
			self._record_leakage_candidate(epc, sku, tag, now)

	def get_state(self) -> dict:
		self._clean_pre_read_buffer(force=True)
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

		risk_data = self.get_corridor_leakage_risk(corredor)
		return {
			'corredor': corredor,
			'corredores_disponiveis': self.get_available_corredores(),
			'buffer_size': self.buffer_size,
			'time_decay': self.time_decay,
			'mode': self.mode,
			'raw_batch_count': len(self.raw_batch_tags),
			'pre_read_count': len(self.pre_read_buffer),
			'leakage_count': len(self.leakage_records),
			'recovered_count': sum(len(v) for v in self.recovered_by_leakage.values()),
			'last_reconcile': self.last_reconcile_summary,
			'leakage_risk': {
				'corredores_vizinhos': risk_data['corredores_vizinhos'],
				'total_enderecos_risco': risk_data['total_enderecos_risco'],
				'total_skus_risco': risk_data['total_skus_risco'],
				'enderecos_risco': risk_data['enderecos_risco'],
			},
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

	@staticmethod
	def _corridor_number(corredor: str) -> Optional[int]:
		m = re.search(r'(\d+)', corredor or '')
		return int(m.group(1)) if m else None

	def _are_adjacent_corridors(self, corr_a: str, corr_b: str) -> bool:
		"""In the CD, odd aisles (129, 131, 133) and even aisles (128, 130, 132) sit on separate sides,
		so physically adjacent back-to-back corridors always differ by exactly 2."""
		num_a = self._corridor_number(corr_a)
		num_b = self._corridor_number(corr_b)
		if num_a is None or num_b is None:
			return corr_a != corr_b
		return abs(num_a - num_b) == 2

	def get_corridor_leakage_risk(self, corredor: str) -> dict:
		"""Pre-processes a corridor against its adjacent (±2) corridors to flag shared SKUs and at-risk addresses."""
		if corredor in self.leakage_risk_cache:
			return self.leakage_risk_cache[corredor]

		adj_corridors = set()
		adj_sku_corridors: Dict[str, set] = {}

		for meta in self.address_catalog.values():
			other_corr = meta['corredor']
			if self._are_adjacent_corridors(corredor, other_corr):
				adj_corridors.add(other_corr)
				for sku in meta.get('skus', {}).keys():
					adj_sku_corridors.setdefault(sku, set()).add(other_corr)

		enderecos_risco = []
		risk_by_address: Dict[str, dict] = {}
		unique_risk_skus = set()

		corridor_items = [m for m in self.address_catalog.values() if m['corredor'] == corredor]
		corridor_items.sort(key=lambda x: (x['secao_num'], x['endereco']))

		for meta in corridor_items:
			addr = meta['endereco']
			skus_em_risco = []
			corrs_risco = set()

			for sku, qtd in meta.get('skus', {}).items():
				if sku in adj_sku_corridors:
					viz_list = sorted(list(adj_sku_corridors[sku]))
					skus_em_risco.append({
						'sku': sku,
						'quantidade': qtd,
						'corredores_vizinhos': viz_list,
					})
					corrs_risco.update(viz_list)
					unique_risk_skus.add(sku)

			if skus_em_risco:
				info = {
					'endereco': addr,
					'secao': meta['secao'],
					'nivel': meta['nivel'],
					'lado': meta['lado'],
					'risco_vazamento': True,
					'corredores_risco': sorted(list(corrs_risco)),
					'skus_em_risco': skus_em_risco,
				}
				enderecos_risco.append(info)
				risk_by_address[addr] = info

		result = {
			'corredores_vizinhos': sorted(list(adj_corridors)),
			'total_enderecos_risco': len(enderecos_risco),
			'total_skus_risco': len(unique_risk_skus),
			'enderecos_risco': enderecos_risco,
			'risk_by_address': risk_by_address,
		}
		self.leakage_risk_cache[corredor] = result
		return result

	def _get_address_leakage_risk(self, corredor: str, addr: str) -> dict:
		risk_data = self.get_corridor_leakage_risk(corredor)
		return risk_data['risk_by_address'].get(
			addr, {'risco_vazamento': False, 'corredores_risco': [], 'skus_em_risco': []}
		)

	def reconcile_leakages(self) -> dict:
		"""Post-processing: pairs opposite addresses in adjacent corridors (±2) using shared EPCs, then recovers missing SKUs."""
		# 1. Discover physical back-to-back pairs via shared EPCs
		# pair_votes[anchor_addr][target_addr] = count of shared EPCs
		pair_votes: Dict[str, Dict[str, int]] = {}
		# section_votes[(anchor_corr, anchor_sec)][(target_corr, target_sec)] = count
		section_votes: Dict[tuple, Dict[tuple, int]] = {}

		for epc, leak in self.leakage_records.items():
			target_addr = self.epc_to_address.get(epc)
			if not target_addr:
				continue
			anchor_addr = leak['endereco_vazamento']
			target_meta = self.address_catalog.get(target_addr)
			anchor_meta = self.address_catalog.get(anchor_addr)
			if not target_meta or not anchor_meta:
				continue
			if not self._are_adjacent_corridors(leak['corredor_leitura'], target_meta['corredor']):
				continue

			pair_votes.setdefault(anchor_addr, {})
			pair_votes[anchor_addr][target_addr] = pair_votes[anchor_addr].get(target_addr, 0) + 1

			sec_key_a = (anchor_meta['corredor'], anchor_meta['secao_num'])
			sec_key_t = (target_meta['corredor'], target_meta['secao_num'])
			section_votes.setdefault(sec_key_a, {})
			section_votes[sec_key_a][sec_key_t] = section_votes[sec_key_a].get(sec_key_t, 0) + 1

		# Build summary of discovered pairs
		pares_ancora = []
		for anchor_addr, targets in pair_votes.items():
			for target_addr, votes in sorted(targets.items(), key=lambda x: -x[1]):
				pares_ancora.append({
					'endereco_vazamento': anchor_addr,
					'endereco_oposto': target_addr,
					'epcs_em_comum': votes,
				})

		# 2. Gather uncounted leaked EPCs sorted by strongest RSSI first
		uncounted_leaks = [
			leak for epc, leak in self.leakage_records.items()
			if epc not in self.seen_tag_epcs
		]
		uncounted_leaks.sort(key=lambda x: x.get('rssi') if x.get('rssi') is not None else -999, reverse=True)

		# Track active slots for instant UI sync
		active_slot_map: Dict[str, dict] = {}
		for queue in self.active_queues.values():
			for slot in queue:
				active_slot_map[slot['endereco']] = slot

		recovered_details = []

		for leak in uncounted_leaks:
			epc = leak['epc']
			sku = leak['sku']
			anchor_addr = leak['endereco_vazamento']
			anchor_meta = self.address_catalog.get(anchor_addr)
			if not anchor_meta:
				continue

			# Build ordered candidate target addresses:
			# Priority 1: Direct address pair confirmed by shared EPCs
			candidates = []
			direct_targets = pair_votes.get(anchor_addr, {})
			for t_addr, _votes in sorted(direct_targets.items(), key=lambda x: -x[1]):
				candidates.append((t_addr, 'Alta (EPC Âncora Direto)'))

			# Priority 2: Section-level pair confirmed by shared EPCs (matching level N1/N2)
			sec_key_a = (anchor_meta['corredor'], anchor_meta['secao_num'])
			sec_targets = section_votes.get(sec_key_a, {})
			for (t_corr, t_sec), _votes in sorted(sec_targets.items(), key=lambda x: -x[1]):
				for addr, meta in self.address_catalog.items():
					if meta['corredor'] == t_corr and meta['secao_num'] == t_sec and meta['nivel'] == anchor_meta['nivel']:
						if not any(c[0] == addr for c in candidates):
							candidates.append((addr, 'Média (Seção Âncora + Nível)'))

			# Try to credit to the best candidate that expects this SKU and still has missing pieces
			for target_addr, confianca in candidates:
				t_meta = self.address_catalog.get(target_addr)
				if not t_meta:
					continue
				expected_skus = t_meta.get('skus', {})
				if sku not in expected_skus and t_meta.get('sku') != sku:
					continue
				curr_lidos = self.read_progress.get(target_addr, 0)
				if curr_lidos >= t_meta['skus_esperados']:
					continue

				# Credit recovered piece
				new_lidos = curr_lidos + 1
				self.read_progress[target_addr] = new_lidos
				self.seen_tag_epcs.add(epc)
				self.epc_to_address[epc] = target_addr

				rec_item = {
					'epc': epc,
					'sku': sku,
					'endereco_destino': target_addr,
					'endereco_vazamento': anchor_addr,
					'corredor_origem': leak['corredor_leitura'],
					'rssi': leak.get('rssi'),
					'confianca': confianca,
				}
				self.recovered_by_leakage.setdefault(target_addr, []).append(rec_item)
				recovered_details.append(rec_item)

				# Sync active slot if currently visible
				if target_addr in active_slot_map:
					slot = active_slot_map[target_addr]
					slot['skus_lidos'] = new_lidos
					pct = round(min(100.0, (new_lidos / slot['skus_esperados'] * 100)), 1) if slot['skus_esperados'] > 0 else 0.0
					slot['progresso'] = pct
					if pct >= 100.0:
						slot['status'] = 'Concluído'
				break

		summary = {
			'total_vazamentos_capturados': len(self.leakage_records),
			'pares_ancora_descobertos': pares_ancora,
			'total_recuperados_nesta_execucao': len(recovered_details),
			'total_recuperados_acumulado': sum(len(v) for v in self.recovered_by_leakage.values()),
			'detalhes_recuperados': recovered_details,
		}
		self.last_reconcile_summary = summary
		return summary

	def get_dashboard_report(self, corredor_filter: Optional[str] = None) -> dict:
		"""Builds complete executive closing report across all or filtered corridors."""
		corredores_disp = self.get_available_corredores()
		target_corr = corredor_filter.strip().upper() if corredor_filter and corredor_filter != 'ALL' else None

		enderecos_rows = []
		corr_map: Dict[str, dict] = {}
		quad_map = {
			'Q1': {'quadrante': 'Q1', 'label': 'Antena 1 • Sup. Esq.', 'lidos': 0, 'esperados': 0, 'concluidos': 0, 'total_enderecos': 0},
			'Q2': {'quadrante': 'Q2', 'label': 'Antena 2 • Inf. Esq.', 'lidos': 0, 'esperados': 0, 'concluidos': 0, 'total_enderecos': 0},
			'Q3': {'quadrante': 'Q3', 'label': 'Antena 3 • Sup. Dir.', 'lidos': 0, 'esperados': 0, 'concluidos': 0, 'total_enderecos': 0},
			'Q4': {'quadrante': 'Q4', 'label': 'Antena 4 • Inf. Dir.', 'lidos': 0, 'esperados': 0, 'concluidos': 0, 'total_enderecos': 0},
		}
		status_counts = {'Concluído': 0, 'Excesso': 0, 'Parcial': 0, 'Não Lido': 0}

		for addr, meta in self.address_catalog.items():
			corr = meta['corredor']
			if target_corr and corr.upper() != target_corr:
				continue

			esperado = int(meta.get('skus_esperados', 0))
			total_lido = int(self.read_progress.get(addr, 0))
			recup_list = self.recovered_by_leakage.get(addr, [])
			recup_count = len(recup_list)
			lido_direto = max(0, total_lido - recup_count)
			divergencia = total_lido - esperado
			excesso = max(0, divergencia)
			falta = max(0, -divergencia)
			pct = round((total_lido / esperado * 100), 1) if esperado > 0 else 0.0

			if total_lido == 0:
				status = 'Não Lido'
			elif total_lido > esperado:
				status = 'Excesso'
			elif total_lido == esperado:
				status = 'Concluído'
			else:
				status = 'Parcial'
			status_counts[status] += 1

			risk_info = self._get_address_leakage_risk(corr, addr)
			risco_flag = bool(risk_info.get('risco_vazamento'))

			enderecos_rows.append({
				'endereco': addr,
				'corredor': corr,
				'secao_num': meta['secao_num'],
				'secao': meta['secao'],
				'nivel': meta['nivel'],
				'lado': meta['lado'],
				'quadrante': meta['quadrante'],
				'esperado': esperado,
				'lido_direto': lido_direto,
				'recuperado_vazamento': recup_count,
				'total_lido': total_lido,
				'divergencia': divergencia,
				'excesso': excesso,
				'falta': falta,
				'pct': pct,
				'risco_vazamento': risco_flag,
				'corredores_risco': risk_info.get('corredores_risco', []),
				'skus_em_risco': [s['sku'] for s in risk_info.get('skus_em_risco', [])],
				'skus_em_risco_count': len(risk_info.get('skus_em_risco', [])),
				'status': status,
			})

			# Corridor aggregation
			if corr not in corr_map:
				corr_map[corr] = {
					'corredor': corr,
					'enderecos_lidos': 0,
					'total_enderecos': 0,
					'esperado': 0,
					'lido_direto': 0,
					'recuperado_vazamento': 0,
					'total_lido': 0,
					'excesso': 0,
					'falta': 0,
					'enderecos_risco': 0,
					'enderecos_excesso': 0,
				}
			c_row = corr_map[corr]
			c_row['total_enderecos'] += 1
			if total_lido > 0:
				c_row['enderecos_lidos'] += 1
			c_row['esperado'] += esperado
			c_row['lido_direto'] += lido_direto
			c_row['recuperado_vazamento'] += recup_count
			c_row['total_lido'] += total_lido
			c_row['excesso'] += excesso
			c_row['falta'] += falta
			if risco_flag:
				c_row['enderecos_risco'] += 1
			if excesso > 0:
				c_row['enderecos_excesso'] += 1

			# Quadrant aggregation
			q_id = meta.get('quadrante', 'Q1')
			if q_id in quad_map:
				quad_map[q_id]['total_enderecos'] += 1
				quad_map[q_id]['esperados'] += esperado
				quad_map[q_id]['lidos'] += min(total_lido, esperado)
				if total_lido >= esperado and esperado > 0:
					quad_map[q_id]['concluidos'] += 1

		enderecos_rows.sort(key=lambda x: (x['corredor'], x['secao_num'], x['endereco']))

		por_corredor = []
		for corr in sorted(corr_map.keys()):
			c_row = corr_map[corr]
			c_row['pct_conclusao'] = round((min(c_row['total_lido'], c_row['esperado']) / c_row['esperado'] * 100), 1) if c_row['esperado'] > 0 else 0.0
			c_row['pct_enderecos'] = round((c_row['enderecos_lidos'] / c_row['total_enderecos'] * 100), 1) if c_row['total_enderecos'] > 0 else 0.0
			por_corredor.append(c_row)

		por_quadrante = []
		for q_id in ['Q1', 'Q2', 'Q3', 'Q4']:
			q = quad_map[q_id]
			q['pct'] = round((q['lidos'] / q['esperados'] * 100), 1) if q['esperados'] > 0 else 0.0
			por_quadrante.append(q)

		total_enderecos = len(enderecos_rows)
		enderecos_lidos = sum(1 for r in enderecos_rows if r['total_lido'] > 0)
		pecas_esperadas = sum(r['esperado'] for r in enderecos_rows)
		pecas_lidas_direto = sum(r['lido_direto'] for r in enderecos_rows)
		pecas_recup = sum(r['recuperado_vazamento'] for r in enderecos_rows)
		pecas_total_lidas = sum(r['total_lido'] for r in enderecos_rows)
		pecas_aderentes = sum(min(r['total_lido'], r['esperado']) for r in enderecos_rows)
		total_pecas_excesso = sum(r['excesso'] for r in enderecos_rows)
		enderecos_com_excesso = sum(1 for r in enderecos_rows if r['excesso'] > 0)
		enderecos_risco = sum(1 for r in enderecos_rows if r['risco_vazamento'])

		return {
			'corredor_filtro': target_corr or 'ALL',
			'corredores_disponiveis': corredores_disp,
			'kpis': {
				'acuracidade_global': round((pecas_aderentes / pecas_esperadas * 100), 1) if pecas_esperadas > 0 else 0.0,
				'enderecos_lidos': enderecos_lidos,
				'total_enderecos': total_enderecos,
				'pct_enderecos_lidos': round((enderecos_lidos / total_enderecos * 100), 1) if total_enderecos > 0 else 0.0,
				'pecas_esperadas': pecas_esperadas,
				'pecas_lidas_direto': pecas_lidas_direto,
				'pecas_recuperadas_vazamento': pecas_recup,
				'pecas_total_lidas': pecas_total_lidas,
				'enderecos_com_excesso': enderecos_com_excesso,
				'total_pecas_excesso': total_pecas_excesso,
				'enderecos_risco_vazamento': enderecos_risco,
				'enderecos_concluidos': status_counts['Concluído'],
				'enderecos_parciais': status_counts['Parcial'],
				'enderecos_nao_lidos': status_counts['Não Lido'],
			},
			'por_corredor': por_corredor,
			'status_distribuicao': status_counts,
			'por_quadrante': por_quadrante,
			'enderecos': enderecos_rows,
		}

	def switch_corredor(self, new_corredor: str):
		"""Switches active corridor while keeping cross-corridor leakage and read history."""
		self.selected_corredor = new_corredor
		self.raw_batch_tags.clear()
		self.pre_read_buffer.clear()
		for q in self.active_queues:
			self.active_queues[q].clear()

	def reset(self):
		self.read_progress.clear()
		self.raw_batch_tags.clear()
		self.pre_read_buffer.clear()
		self.seen_tag_epcs.clear()
		self.epc_to_address.clear()
		self.leakage_records.clear()
		self.recovered_by_leakage.clear()
		self.last_reconcile_summary = None
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
		carrinho_engine.switch_corredor(payload.corredor)
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


@router.post('/reconcile_leakages')
async def reconcile_leakages():
	summary = carrinho_engine.reconcile_leakages()
	state = carrinho_engine.get_state()
	return JSONResponse(status_code=200, content={'summary': summary, 'state': state})


@router.get('/dashboard')
async def get_dashboard(corredor: Optional[str] = None):
	return JSONResponse(status_code=200, content=carrinho_engine.get_dashboard_report(corredor))


@router.post('/reset')
async def reset_carrinho():
	carrinho_engine.reset()
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())


@router.post('/reload_database')
async def reload_database():
	carrinho_engine._init_data()
	return JSONResponse(status_code=200, content=carrinho_engine.get_state())
