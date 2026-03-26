import logging

_logger = logging.getLogger(__name__)


class MessageAPI:
    def __init__(self, client):
        self.client = client

    def _url(self, path=''):
        return f"https://{self.client.api_domain}/api/v1/messages{path}"

    def send(self, sender_fulfillment_id, receiver_fulfillment_id, content,
             ref_type=None, ref_id=None):
        """Send a message from this instance to a partner instance."""
        payload = {
            'sender_fulfillment_id': sender_fulfillment_id,
            'receiver_fulfillment_id': receiver_fulfillment_id,
            'content': content,
        }
        if ref_type:
            payload['ref_type'] = ref_type
        if ref_id:
            payload['ref_id'] = ref_id
        return self.client._request('POST', self._url(), payload=payload)

    def fetch(self, fulfillment_id, partner_id, since=None, limit=50,
              ref_type=None, ref_id=None):
        """Fetch conversation thread between two fulfillment instances."""
        params = {
            'fulfillment_id': fulfillment_id,
            'partner_id': partner_id,
            'limit': limit,
        }
        if since:
            params['since'] = since
        if ref_type:
            params['ref_type'] = ref_type
        if ref_id:
            params['ref_id'] = ref_id
        return self.client._request('GET', self._url(), params=params)

    def mark_read(self, message_ids, fulfillment_id):
        """Mark a list of messages as read."""
        return self.client._request('PATCH', self._url('/read'), payload={
            'message_ids': message_ids,
            'fulfillment_id': fulfillment_id,
        })

    def count_unread(self, fulfillment_id, partner_id=None):
        """Get unread message count for this fulfillment."""
        params = {'fulfillment_id': fulfillment_id}
        if partner_id:
            params['partner_id'] = partner_id
        return self.client._request('GET', self._url('/unread'), params=params)
