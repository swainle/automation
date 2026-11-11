"""Capture product JSON on its own Page before navigation, excluding unrelated goods."""
from urllib.parse import parse_qs, urlsplit


def valid_goods_id(value):
    text = str(value or '')
    return text if text.isascii() and text.isdecimal() and int(text) > 0 else ''


def goods_id_from_url(url):
    query = parse_qs(urlsplit(url).query)
    return valid_goods_id(query.get('goods_id', query.get('goodsID', ['']))[0])


def discover_goods_id(responses):
    """Use only primary product envelopes, never recursive recommendation IDs."""
    candidates = set()
    for item in responses:
        parts = urlsplit(item.get('url', ''))
        host, path = parts.hostname or '', parts.path.lower()
        if not any(host == domain or host.endswith('.' + domain) for domain in ('yangkeduo.com', 'pinduoduo.com')):
            continue
        if (item.get('status', 500) >= 400 or item.get('type', '').lower() not in {'xhr', 'fetch'}
                or not any(word in path for word in ('goods', 'oak', 'product'))
                or any(word in path for word in ('recommend', 'related', 'search', 'review', 'feed'))):
            continue
        payload = item.get('data')
        if not isinstance(payload, dict) or payload.get('success') is False or payload.get('error_code') not in (None, 0, '0'):
            continue
        # Deliberately bounded paths: recommendations under lists are not identity.
        envelopes = [payload]
        if isinstance(payload.get('data'), dict):
            envelopes.append(payload['data'])
        primary = list(envelopes)
        for envelope in envelopes:
            primary.extend(envelope[key] for key in ('goods', 'product', 'goods_info', 'goodsInfo')
                           if isinstance(envelope.get(key), dict))
        response_ids = {valid_goods_id(value.get('goods_id', value.get('goodsID'))) for value in primary}
        response_ids.discard('')
        requested = goods_id_from_url(item['url'])
        body = item.get('body')
        body_id = valid_goods_id(body.get('goods_id', body.get('goodsID'))) if isinstance(body, dict) else ''
        request_ids = {value for value in (requested, body_id) if value}
        if len(response_ids | request_ids) > 1:
            raise RuntimeError('商品信息接口存在冲突 ID，保留待重试')
        candidates.update(response_ids or request_ids)
    if len(candidates) > 1:
        raise RuntimeError('无法从商品信息接口确认唯一商品 ID，保留待重试')
    return next(iter(candidates), '')


def goods_objects(value):
    if isinstance(value, dict):
        if value.get('goods_id') is not None or value.get('goodsID') is not None:
            yield value
        for item in value.values():
            yield from goods_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from goods_objects(item)


class ProductInfo:
    def __init__(self, goods_id):
        self.goods_id = str(goods_id)
        self.responses = []
        self.page = None
        self.errors = []

    def attach(self, page):
        self.page = page
        page.on('response', self.receive)

    def receive(self, response):
        parts = urlsplit(response.url)
        host = parts.hostname or ''
        if not any(host == domain or host.endswith('.' + domain) for domain in ('yangkeduo.com', 'pinduoduo.com')):
            return
        if response.status >= 400 or response.request.resource_type not in {'xhr', 'fetch'}:
            return
        try:
            payload = response.json()
        except (ValueError, RuntimeError):
            return
        except Exception as error:
            self.errors.append(type(error).__name__)
            return
        if isinstance(payload, dict) and (payload.get('success') is False or payload.get('error_code') not in (None, 0, '0')):
            return
        # A recommendation response may contain the target too; persist only the
        # matching goods object, never attach another product's metadata.
        matches = [obj for obj in goods_objects(payload)
                   if str(obj.get('goods_id', obj.get('goodsID'))) == self.goods_id]
        if matches:
            self.responses.append({'endpoint': parts.path, 'goods': matches})
        elif isinstance(payload, dict) and any(word in parts.path.lower() for word in ('goods', 'oak', 'product')):
            # Some product APIs put the identity only in the request envelope.
            query = parse_qs(parts.query)
            requested = query.get('goods_id', query.get('goodsID', []))
            try:
                body = response.request.post_data_json
            except Exception:
                body = None
            requested += [str(item.get('goods_id', item.get('goodsID'))) for item in goods_objects(body)]
            # A response naming a different product must never use this fallback.
            if self.goods_id in requested and not list(goods_objects(payload)) and payload:
                self.responses.append({'endpoint': parts.path, 'data': payload})

    def finish(self):
        if self.page is not None:
            self.page.remove_listener('response', self.receive)
            self.page = None
        if not self.responses:
            raise RuntimeError('未监听到当前商品的产品信息响应，保留待重试')
        return {'goods_id': self.goods_id, 'responses': self.responses}

    def close(self):
        if self.page is not None:
            self.page.remove_listener('response', self.receive)
            self.page = None
