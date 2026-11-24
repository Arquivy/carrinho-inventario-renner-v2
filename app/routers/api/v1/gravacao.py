import asyncio
import json
import logging
import os
from typing import List, Optional
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.models.rfid import Inventory
from app.services import rfid_manager
from app.services.rfid.renner_codec import (
	get_renner_sku,
	renner_get_address_by_epc,
	renner_get_epc_from_sku,
	renner_serialize_epc_by_address,
)
from smartx_rfid.schemas.tag import WriteTagValidator

router = APIRouter(prefix='/api/v1/gravacao', tags=['Gravacao'])


# ===================== POWER CONTROLS =====================
current_power_config = {
	'read_power': 30.0,
	'write_power': 18.0,
	'min_power': 10.0,
	'max_power': 33.0,
}

# Try loading read_power from device config if available
try:
	cfg_path = os.path.join('config', 'devices', 'impinj-17-39-4a.json')
	if os.path.exists(cfg_path):
		with open(cfg_path, 'r', encoding='utf-8') as f:
			dev_cfg = json.load(f)
			raw_pwr = dev_cfg.get('read_power', 30)
			current_power_config['read_power'] = float(raw_pwr if raw_pwr <= 33 else raw_pwr / 100)
except Exception:
	pass


class PowerSettingsPayload(BaseModel):
	read_power: Optional[float] = None
	write_power: Optional[float] = None


async def apply_device_power(power_dbm: float):
	"""Sets transmit power on all active antennas of the reader (R700_IOT REST and R420 LLRP)."""
	cdbm = int(round(power_dbm * 100))
	cdbm = max(1000, min(3300, cdbm))
	devices = rfid_manager.devices.get_devices()
	for dev in devices:
		if hasattr(dev, 'reading_config') and 'antennaConfigs' in dev.reading_config:
			for ant in dev.reading_config['antennaConfigs']:
				ant['transmitPowerCdbm'] = cdbm
			if getattr(dev, 'is_reading', False):
				try:
					logging.info(f"[POTENCIA] Atualizando potência REST de {dev.name} para {power_dbm} dBm ({cdbm} cdBm)...")
					await dev.stop_inventory()
					await asyncio.sleep(0.15)
					await dev.start_inventory()
				except Exception as e:
					logging.error(f"Erro ao reiniciar inventário com nova potência: {e}")
		elif hasattr(dev, 'read_power') and hasattr(dev, '_client'):
			try:
				logging.info(f"[POTENCIA] Atualizando potência LLRP de {dev.name} para {power_dbm} dBm...")
				dev.read_power = float(power_dbm)
				if getattr(dev, '_client', None) is not None and getattr(dev._client, 'llrp', None) is not None:
					dev._client.config.tx_power_dbm = float(power_dbm)
					if getattr(dev, 'is_reading', False):
						await dev.stop_inventory(check_gpi=False)
						dev._client.llrp.startInventory(force_regen_rospec=True)
						dev.is_reading = True
			except Exception as e:
				logging.error(f"Erro ao atualizar potência LLRP: {e}")


def persist_read_power(power_dbm: float):
	"""Saves read power setting into device config JSON."""
	try:
		cfg_path = os.path.join('config', 'devices', 'impinj-17-39-4a.json')
		if os.path.exists(cfg_path):
			with open(cfg_path, 'r', encoding='utf-8') as f:
				dev_cfg = json.load(f)
			dev_cfg['read_power'] = round(power_dbm, 1)
			with open(cfg_path, 'w', encoding='utf-8') as f:
				json.dump(dev_cfg, f, indent=2)
	except Exception as e:
		logging.warning(f"Could not persist read power to config: {e}")


@router.get('/power_settings')
async def get_power_settings():
	"""Gets current reading and writing power levels in dBm."""
	return current_power_config


@router.post('/power_settings')
async def update_power_settings(payload: PowerSettingsPayload):
	"""Updates reading and writing power levels and applies to reader."""
	read_changed = False
	if payload.read_power is not None:
		val = max(10.0, min(33.0, round(float(payload.read_power), 1)))
		if abs(val - current_power_config['read_power']) > 0.05:
			current_power_config['read_power'] = val
			read_changed = True
			persist_read_power(val)

	if payload.write_power is not None:
		current_power_config['write_power'] = max(10.0, min(33.0, round(float(payload.write_power), 1)))

	if read_changed:
		await apply_device_power(current_power_config['read_power'])

	return current_power_config


# ===================== PAYLOADS =====================
class WriteAddressPayload(BaseModel):
	endereco: str
	target_tid: str
	device_name: Optional[str] = None


class SkuBatchItem(BaseModel):
	sku: str
	serial: int
	target_tid: str
	target_epc: Optional[str] = None


class WriteSkuBatchPayload(BaseModel):
	endereco: str
	device_name: Optional[str] = None
	items: List[SkuBatchItem]


def get_active_device_name(preferred: Optional[str] = None) -> Optional[str]:
	"""Returns the first connected device name or preferred device if valid."""
	try:
		info_list = rfid_manager.devices.get_device_info()
		if preferred:
			for d in info_list:
				if d.get('name') == preferred and d.get('is_connected'):
					return preferred
		for d in info_list:
			if d.get('is_connected'):
				return d.get('name')
		devices = rfid_manager.devices.get_devices()
		if devices:
			return devices[0].name
	except Exception as e:
		logging.error(f'Error getting active device: {e}')
	return None


@router.get('/corredores')
async def get_corredores():
	"""Lists all corridors with inventory items in the database."""
	try:
		corredores = set()
		db = rfid_manager.controller.integration.db_manager
		items = db.get_all(Inventory)
		if items:
			for it in items:
				if it.corredor:
					c = str(it.corredor).strip()
					if not c.startswith('C'):
						c = f'C{c}'
					corredores.add(c)
		return {'corredores': sorted(list(corredores))}
	except Exception as e:
		return JSONResponse(status_code=500, content={'error': str(e)})


@router.get('/enderecos/{corredor}')
async def get_enderecos_by_corredor(corredor: str):
	"""Returns all distinct addresses for a given corridor with item stats."""
	try:
		db = rfid_manager.controller.integration.db_manager
		items = db.get_all(Inventory)
		addr_map = {}
		target_corredor = corredor.strip().upper()
		for it in items:
			curr_corr = (it.corredor or '').strip().upper()
			if curr_corr == target_corredor or curr_corr.lstrip('C') == target_corredor.lstrip('C'):
				addr = it.endereco
				if addr not in addr_map:
					addr_map[addr] = {
						'endereco': addr,
						'corredor': it.corredor,
						'skus': [],
						'total_quantidade': 0,
					}
				addr_map[addr]['skus'].append({
					'sku': it.sku,
					'quantidade': int(it.quantidade or 1),
				})
				addr_map[addr]['total_quantidade'] += int(it.quantidade or 1)

		addresses_list = list(addr_map.values())
		addresses_list.sort(key=lambda x: x['endereco'])
		return {'corredor': corredor, 'enderecos': addresses_list}
	except Exception as e:
		return JSONResponse(status_code=500, content={'error': str(e)})


@router.get('/endereco_detalhes')
async def get_endereco_detalhes(corredor: str, endereco: str):
	"""Generates the Address EPC and SKU targets with sequential serials."""
	try:
		# 1. Generate Address EPC
		address_epc = None
		address_epc_error = None
		try:
			address_epc = renner_serialize_epc_by_address(endereco)
		except Exception as e:
			address_epc_error = str(e)

		# 2. Get SKUs from DB
		db = rfid_manager.controller.integration.db_manager
		items = db.get_all(Inventory)
		matched_items = []
		total_pecas = 0
		serial_counter = 1

		sku_targets = []
		for it in items:
			if it.endereco == endereco:
				qtd = int(it.quantidade or 1)
				total_pecas += qtd
				matched_items.append({'sku': it.sku, 'quantidade': qtd})
				for i in range(qtd):
					try:
						epc_gerado = renner_get_epc_from_sku(it.sku, serial=serial_counter)
					except Exception:
						epc_gerado = f"0000{int(it.sku):012x}{serial_counter:08x}"
					sku_targets.append({
						'item_idx': len(sku_targets) + 1,
						'sku': it.sku,
						'serial': serial_counter,
						'epc_target': epc_gerado,
						'status': 'Pendente',
					})
					serial_counter += 1

		return {
			'corredor': corredor,
			'endereco': endereco,
			'address_epc': address_epc,
			'address_epc_error': address_epc_error,
			'skus_resumo': matched_items,
			'total_pecas': total_pecas,
			'sku_targets': sku_targets,
		}
	except Exception as e:
		return JSONResponse(status_code=500, content={'error': str(e)})


@router.get('/tags_na_bancada')
async def get_tags_na_bancada():
	"""Gets currently detected tags in memory with their TIDs and EPCs."""
	try:
		tags = rfid_manager.tags.get_all()
		formatted = []
		for t in tags:
			epc = t.get('epc', '')
			tid = t.get('tid', '')
			sku = get_renner_sku(epc)
			addr = renner_get_address_by_epc(epc)
			tag_type = 'Tag de Endereço' if addr else ('Produto Renner' if sku else 'Tag RFID Comum')
			formatted.append({
				'tid': tid,
				'epc': epc,
				'rssi': t.get('rssi'),
				'ant': t.get('ant'),
				'device': t.get('device'),
				'tag_type': tag_type,
				'decoded_info': addr if addr else (f"SKU: {sku}" if sku else None),
			})
		return {'total': len(formatted), 'tags': formatted}
	except Exception as e:
		return JSONResponse(status_code=500, content={'error': str(e)})


@router.post('/clear_tags_bancada')
async def clear_tags_bancada():
	"""Clears detected tags in memory for a clean workbench reading."""
	rfid_manager.tags.clear()
	return {'message': 'Tags da bancada limpas com sucesso!'}


@router.post('/toggle_reading')
async def toggle_reading():
	"""Starts or stops inventory reading on connected readers."""
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


@router.post('/gravar_tag_endereco')
async def gravar_tag_endereco(payload: WriteAddressPayload):
	"""Encodes warehouse address into EPC and writes to tag anchored by TID, applying write power."""
	try:
		epc_target = renner_serialize_epc_by_address(payload.endereco)
	except Exception as e:
		return JSONResponse(status_code=400, content={'error': f'Erro ao serializar endereço: {e}'})

	if not payload.target_tid:
		return JSONResponse(status_code=400, content={'error': 'TID alvo não fornecido'})

	device_name = get_active_device_name(payload.device_name)
	if not device_name:
		return JSONResponse(status_code=400, content={'error': 'Nenhum leitor conectado para gravação'})

	validator = WriteTagValidator(
		target_identifier='tid',
		target_value=payload.target_tid.lower().strip(),
		new_epc=epc_target.lower().strip(),
		password='00000000',
	)

	# Apply write power before programming tag
	write_pwr = current_power_config.get('write_power', 18.0)
	read_pwr = current_power_config.get('read_power', 30.0)
	needs_power_switch = abs(write_pwr - read_pwr) > 0.1

	if needs_power_switch:
		await apply_device_power(write_pwr)

	try:
		logging.info(f'[BANCADA] Gravando Endereço: {payload.endereco} -> EPC: {epc_target} no TID: {payload.target_tid} com potência {write_pwr} dBm')
		status, msg = await rfid_manager.devices.write_epc(device_name, validator)
	finally:
		if needs_power_switch:
			await apply_device_power(read_pwr)

	if not status:
		return JSONResponse(status_code=400, content={'error': msg or 'Falha ao gravar EPC de endereço'})

	return {
		'status': 'success',
		'message': 'Tag de endereço gravada com sucesso!',
		'endereco': payload.endereco,
		'target_tid': payload.target_tid,
		'epc_gravado': epc_target,
		'device': device_name,
		'potencia_gravacao': write_pwr,
	}


@router.post('/gravar_skus_lote')
async def gravar_skus_lote(payload: WriteSkuBatchPayload):
	"""Writes a batch of SKU EPCs anchored to individual TIDs with write power control."""
	device_name = get_active_device_name(payload.device_name)
	if not device_name:
		return JSONResponse(status_code=400, content={'error': 'Nenhum leitor conectado para gravação'})

	write_pwr = current_power_config.get('write_power', 18.0)
	read_pwr = current_power_config.get('read_power', 30.0)
	needs_power_switch = abs(write_pwr - read_pwr) > 0.1

	if needs_power_switch:
		await apply_device_power(write_pwr)

	results = []
	try:
		for item in payload.items:
			try:
				epc = item.target_epc or renner_get_epc_from_sku(item.sku, item.serial)
				tid = item.target_tid.lower().strip()
				validator = WriteTagValidator(
					target_identifier='tid',
					target_value=tid,
					new_epc=epc.lower().strip(),
					password='00000000',
				)
				logging.info(f'[BANCADA] Gravando SKU {item.sku} (S/N {item.serial}) -> EPC {epc} no TID {tid} ({write_pwr} dBm)')
				status, msg = await rfid_manager.devices.write_epc(device_name, validator)
				results.append({
					'sku': item.sku,
					'serial': item.serial,
					'tid': tid,
					'epc': epc,
					'status': 'Sucesso' if status else 'Erro',
					'message': msg if not status else 'OK',
				})
			except Exception as e:
				results.append({
					'sku': item.sku,
					'serial': item.serial,
					'tid': item.target_tid,
					'status': 'Erro',
					'message': str(e),
				})
	finally:
		if needs_power_switch:
			await apply_device_power(read_pwr)

	success_count = sum(1 for r in results if r['status'] == 'Sucesso')
	return {
		'status': 'completed',
		'total': len(results),
		'sucessos': success_count,
		'falhas': len(results) - success_count,
		'detalhes': results,
		'potencia_gravacao': write_pwr,
	}
