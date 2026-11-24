import re
from smartx_rfid.utils import regex_hex


def get_renner_sku(epc: str) -> str | None:
	"""Decodes 24-hex EPC to Renner SKU."""
	if not epc or len(epc) < 16:
		return None
	try:
		if not regex_hex(epc, 24):
			return None
		# SKU is stored in bytes 2..7 (hex chars 4..16)
		sku_int = int(epc[4:16], 16)
		return str(sku_int) if sku_int > 0 else None
	except Exception:
		return None


def renner_get_epc_from_sku(sku: str, serial: int = 1) -> str:
	"""Encodes Renner SKU + sequential serial into 24-hex EPC (96-bit).

	Format: 4 hex '0000' + 12 hex SKU + 8 hex Serial = 24 hex.
	"""
	sku_val = int(re.sub(r'\D', '', str(sku)))
	serial_val = int(serial)
	if not 0 <= sku_val < 16**12:
		raise ValueError('SKU excede 12 caracteres hexadecimais')
	if not 0 <= serial_val < 16**8:
		raise ValueError('Serial excede 8 caracteres hexadecimais')
	return f'0000{sku_val:012x}{serial_val:08x}'.lower()


def renner_serialize_epc_by_address(address: str) -> str:
	"""Encodes Renner physical warehouse address (e.g.

	EN0.C131.S002.N1.0) into 24-hex EPC starting with 'edc0'.
	"""
	parts = address.strip().split('.')
	if len(parts) != 5:
		# Try splitting by hyphen or dash
		parts = re.split(r'[.-]', address.strip())
		if len(parts) != 5:
			raise ValueError(f'Formato de endereço inválido: {address}. Esperado EN0.C131.S002.N1.0')

	prefix = 'edc0'
	andar_str = re.sub(r'\D', '', parts[0]).zfill(4)
	corredor_str = re.sub(r'\D', '', parts[1]).zfill(4)
	secao_str = re.sub(r'\D', '', parts[2]).zfill(4)
	nivel_str = re.sub(r'\D', '', parts[3]).zfill(4)
	sobra_str = re.sub(r'\D', '', parts[4]).zfill(4)

	epc = prefix + andar_str + corredor_str + secao_str + nivel_str + sobra_str
	if len(epc) != 24:
		raise ValueError(f'Erro ao serializar endereço {address}: tamanho gerado {len(epc)}')
	return epc.lower()


def renner_get_address_by_epc(epc: str) -> str | None:
	"""Decodes 24-hex EPC starting with 'edc0' back to Renner address."""
	if not epc or len(epc) != 24:
		return None
	epc_clean = epc.lower().strip()
	if not epc_clean.startswith('edc0'):
		return None
	if not regex_hex(epc_clean, 24):
		return None

	try:
		parts_hex = [epc_clean[idx : idx + 4] for idx in range(4, 24, 4)]
		andar = 'EN' + (parts_hex[0].lstrip('0') or '0')
		corredor = 'C' + parts_hex[1].lstrip('0').zfill(3)
		secao = 'S' + parts_hex[2].lstrip('0').zfill(3)
		nivel = 'N' + (parts_hex[3].lstrip('0') or '0')
		sobra = parts_hex[4].lstrip('0') or '0'
		return f'{andar}.{corredor}.{secao}.{nivel}.{sobra}'
	except Exception:
		return None
