"""Security regression tests for external AI endpoint resolution and connection."""
import socket
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from scheduling.services.ai_configuration import chat_completion, check_external_host
from scheduling.services.common import BusinessError


def address(ip, port=443):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, 6, '', (ip, port))


class AIEndpointSecurityTests(SimpleTestCase):
    def test_non_public_and_mixed_dns_answers_are_blocked(self):
        cases = [
            ['127.0.0.1'], ['10.1.2.3'], ['172.16.0.1'], ['192.168.1.1'],
            ['169.254.169.254'], ['192.0.2.1'], ['198.18.0.35'], ['::1'],
            ['fc00::1'], ['fe80::1'], ['2001:db8::1'], ['8.8.8.8', '127.0.0.1'],
            ['8.8.8.8', '198.18.0.35'], ['224.0.0.1'], ['ff02::1'],
        ]
        for ips in cases:
            with self.subTest(ips=ips), \
                 patch('scheduling.services.ai_configuration.socket.getaddrinfo',
                       return_value=[address(ip) for ip in ips]), \
                 patch('scheduling.services.ai_configuration.httpx.get') as doh:
                with self.assertRaises(BusinessError):
                    check_external_host('https://example.com/v1', 'openai_compatible')
                doh.assert_not_called()

    def test_fake_ip_fallback_is_limited_to_deepseek_exact_host(self):
        variants = [
            ('https://example.com/v1', 'openai_compatible'),
            ('https://api.deepseek.com.example.com/v1', 'deepseek'),
            ('https://api.deepseek.com/v1', 'openai_compatible'),
        ]
        for url, provider in variants:
            with self.subTest(url=url, provider=provider), \
                 patch('scheduling.services.ai_configuration.socket.getaddrinfo',
                       return_value=[address('198.18.0.35')]), \
                 patch('scheduling.services.ai_configuration.httpx.get') as doh:
                with self.assertRaises(BusinessError):
                    check_external_host(url, provider)
                doh.assert_not_called()

    def test_trusted_dns_fallback_requires_public_answer(self):
        with patch('scheduling.services.ai_configuration.socket.getaddrinfo',
                   return_value=[address('198.18.0.35')]), \
             patch('scheduling.services.ai_configuration.httpx.get') as doh:
            remote = Mock()
            doh.return_value = remote
            remote.json.return_value = {'Status': 0, 'Answer': [{'type': 1, 'data': '8.8.8.8'}]}
            self.assertEqual(check_external_host('https://api.deepseek.com/v1', 'deepseek'), '8.8.8.8')
            doh.assert_called_once_with('https://dns.alidns.com/resolve',
                params={'name': 'api.deepseek.com', 'type': 'A'}, timeout=5,
                follow_redirects=False, trust_env=False)
            for answer in ([], [{'type': 1, 'data': '127.0.0.1'}],
                           [{'type': 1, 'data': '8.8.8.8'}, {'type': 1, 'data': '10.0.0.1'}],
                           [{'type': 1, 'data': 'not-an-ip'}]):
                remote.json.return_value = {'Status': 0, 'Answer': answer}
                with self.subTest(answer=answer), self.assertRaises(BusinessError):
                    check_external_host('https://api.deepseek.com/v1', 'deepseek')

    def test_actual_connection_uses_checked_ip_with_original_host_and_sni(self):
        settings = {'provider': 'openai_compatible', 'base_url': 'https://example.com:8443/v1',
                    'model': 'synthetic-model'}
        remote = Mock()
        remote.json.return_value = {'choices': [{'finish_reason':'stop', 'message': {'content': 'OK'}}]}
        with patch('scheduling.services.ai_configuration.socket.getaddrinfo',
                   side_effect=[[address('8.8.8.8', 8443)], [address('127.0.0.1', 8443)]]) as dns, \
             patch('scheduling.services.ai_configuration.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value = remote
            result = chat_completion(settings, 'synthetic-key', [{'role': 'user', 'content': 'test'}])
        self.assertEqual(result, 'OK')
        self.assertEqual(dns.call_count, 1, '连接前不得第二次解析域名，避免 DNS 重绑定')
        client.assert_called_once_with(timeout=30, follow_redirects=False, trust_env=False)
        args, kwargs = client.return_value.__enter__.return_value.post.call_args
        self.assertEqual(str(args[0]), 'https://8.8.8.8:8443/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Host'], 'example.com:8443')
        self.assertEqual(kwargs['extensions']['sni_hostname'], 'example.com')

    def test_ipv6_public_address_is_bracketed_and_keeps_original_tls_name(self):
        settings = {'provider': 'openai_compatible', 'base_url': 'https://example.com/v1',
                    'model': 'synthetic-model'}
        remote = Mock()
        remote.json.return_value = {'choices': [{'finish_reason':'stop', 'message': {'content': 'OK'}}]}
        with patch('scheduling.services.ai_configuration.socket.getaddrinfo',
                   return_value=[address('2001:4860:4860::8888')]), \
             patch('scheduling.services.ai_configuration.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value = remote
            self.assertEqual(chat_completion(settings, 'synthetic-key', []), 'OK')
        args, kwargs = client.return_value.__enter__.return_value.post.call_args
        self.assertEqual(str(args[0]), 'https://[2001:4860:4860::8888]/v1/chat/completions')
        self.assertEqual(kwargs['headers']['Host'], 'example.com')
        self.assertEqual(kwargs['extensions']['sni_hostname'], 'example.com')

