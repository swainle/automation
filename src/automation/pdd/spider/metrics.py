"""One flat, display-text metric schema shared by browser and iOS collectors."""
import re

FIELDS = ('display_price', 'recent_sales', 'total_sales', 'hot_index', 'dominance_days',
          'positive_rate', 'positive_count', 'coupon_price', 'deal_price_30d', 'price_drop')
LEGACY = {'近期拼单': 'recent_sales', '展示价': 'display_price', '24小时内拼单': 'recent_sales', '销售数量': 'total_sales',
          '热卖指数': 'hot_index', '霸榜天数': 'dominance_days', '好评率': 'positive_rate',
          '好评人数': 'positive_count', '券后价': 'coupon_price', '近30天成交价': 'deal_price_30d', '降价': 'price_drop'}


def from_text(text):
    result = dict.fromkeys(FIELDS)
    patterns = {
        'recent_sales': r'(?:近|过去)?(?:24小时|一周|7天|30[天日]|一个月)(?:内)?\s*[\d.]+\s*[万亿]?\+?\s*(?:人拼单|人已拼|人购买|件|单)',
        'total_sales': r'(?:疯抢|已抢|总销|总售|已售|已销|累计售出|售出)\s*[\d.]+\s*[万亿]?\+?\s*件',
        'hot_index': r'热卖指数\s*[\d.]+',
        'dominance_days': r'(?:蝉联榜首|霸榜前三|霸榜|蝉联)\s*\d+\s*天',
        'positive_rate': r'好评率\s*[\d.]+\s*%',
        'positive_count': r'(?:[\d.]+\s*[万亿]?\+?\s*人好评|好评人数\s*[\d.]+\s*[万亿]?\+?)',
        'coupon_price': r'(?:券后|卷后)\s*[¥￥]\s*[\d.]+(?:起)?',
        'deal_price_30d': r'近\s*30\s*[日天]\s*成交价\s*[¥￥]?\s*\d+(?:\.\d+)?',
        'price_drop': r'直降\s*[¥￥]?\s*\d+(?:\.\d+)?',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            result[key] = match.group(0).strip()
    excluded = [match.span() for key in ('coupon_price', 'deal_price_30d', 'price_drop')
                for match in re.finditer(patterns[key], text)]
    for match in re.finditer(r'[¥￥]\s*\d+(?:\.\d+)?(?:起)?', text):
        if not any(start <= match.start() < end for start, end in excluded):
            result['display_price'] = match.group(0).strip()
            break
    return result


def from_row(row):
    result = dict.fromkeys(FIELDS)
    for old, key in LEGACY.items():
        if row.get(old) not in (None, ''):
            result[key] = str(row[old])
    if 'metrics' not in row and not result['total_sales'] and row.get('销量') not in (None, ''):
        result['total_sales'] = str(row['销量'])
    if 'metrics' not in row and not result['display_price'] and row.get('价格') not in (None, ''):
        result['display_price'] = str(row['价格'])
    for key, value in row.get('metrics', {}).items():
        key = 'recent_sales' if key == 'sales_24h' else key
        if key in result:
            result[key] = str(value) if value not in (None, '') else None
    return result
