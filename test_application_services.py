#!/usr/bin/env python
"""
Laboratorio 05 - Actividad 5: pruebas de la capa de SERVICIOS DE APLICACION
con dobles de prueba (unittest.mock).

Servicios probados (todos orquestan dependencias externas):
  * app/tasks.py          -> export_posts / _set_task_progress
                             (dependencias: db.session [repositorio], rq job, send_email)
  * app/translate.py      -> translate (dependencia: requests.post)
  * app/email.py          -> send_email / send_async_email (dependencias: mail, Thread)
  * app/auth/email.py     -> send_password_reset_email (dependencias: send_email, User)
  * app/search.py         -> add_to_index / remove_from_index / query_index
                             (dependencia: Elasticsearch)

Ejecucion:
    python -m unittest test_application_services -v
    coverage run -m unittest test_application_services && coverage report -m
"""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import app as app_pkg
from app import db
from app.models import User, Task
from config import Config


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite://'
    ELASTICSEARCH_URL = None
    MS_TRANSLATOR_KEY = 'fake-key'
    ADMINS = ['admin@example.com']


# app/tasks.py ejecuta create_app() y empuja un app_context al IMPORTARSE.
# Lo importamos con create_app reemplazado para que use la config de pruebas
# (BD en memoria, sin logs en disco ni conexiones reales).
_flask_app = app_pkg.create_app(TestConfig)
with patch('app.create_app', return_value=_flask_app):
    from app import tasks


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
class AppContextTestCase(unittest.TestCase):
    """Base: crea la app de pruebas y un contexto de peticion (necesario
    para current_app y para flask_babel `_()`)."""

    def setUp(self):
        self.app = _flask_app
        self.ctx = self.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()


# ===========================================================================
# 1. tasks.export_posts  (servicio con "repositorio" db.session doblado)
# ===========================================================================
class ExportPostsServiceTest(AppContextTestCase):
    """Pruebas unitarias aisladas: db, sa, rq, mail, render_template y
    time.sleep se reemplazan por dobles."""

    def setUp(self):
        super().setUp()
        # --- Dobles del "repositorio" (db.session) ---
        self.user = MagicMock(name='user', email='lu@example.com')
        self.posts = [
            MagicMock(body=f'post {i}',
                      timestamp=datetime(2026, 1, i, 12, 0, 0))
            for i in (1, 2, 3)
        ]
        self.db_patcher = patch.object(tasks, 'db')
        self.mock_db = self.db_patcher.start()
        self.mock_db.session.get.return_value = self.user
        self.mock_db.session.scalar.return_value = len(self.posts)
        self.mock_db.session.scalars.return_value = iter(self.posts)

        # --- Otros colaboradores ---
        self.sa_patcher = patch.object(tasks, 'sa')
        self.sa_patcher.start()
        self.time_patcher = patch.object(tasks, 'time')
        self.mock_time = self.time_patcher.start()
        self.email_patcher = patch.object(tasks, 'send_email')
        self.mock_send_email = self.email_patcher.start()
        self.render_patcher = patch.object(
            tasks, 'render_template', side_effect=lambda tpl, **kw: f'<{tpl}>')
        self.mock_render = self.render_patcher.start()
        self.progress_patcher = patch.object(tasks, '_set_task_progress')
        self.mock_progress = self.progress_patcher.start()

    def tearDown(self):
        patch.stopall()
        super().tearDown()

    def test_export_sends_email_with_all_posts_attached(self):
        tasks.export_posts(user_id=7)

        # se consulto el repositorio con el id correcto
        self.mock_db.session.get.assert_called_once_with(tasks.User, 7)
        # se envio exactamente un correo, sincrono, al email del usuario
        self.mock_send_email.assert_called_once()
        args, kwargs = self.mock_send_email.call_args
        self.assertEqual(args[0], '[Microblog] Your blog posts')
        self.assertEqual(kwargs['sender'], 'admin@example.com')
        self.assertEqual(kwargs['recipients'], ['lu@example.com'])
        self.assertTrue(kwargs['sync'])
        # el adjunto es JSON con los 3 posts, con timestamp ISO + 'Z'
        name, mime, payload = kwargs['attachments'][0]
        self.assertEqual((name, mime), ('posts.json', 'application/json'))
        data = json.loads(payload)
        self.assertEqual([p['body'] for p in data['posts']],
                         ['post 1', 'post 2', 'post 3'])
        self.assertEqual(data['posts'][0]['timestamp'], '2026-01-01T12:00:00Z')

    def test_export_renders_both_email_templates_for_the_user(self):
        tasks.export_posts(user_id=1)

        self.mock_render.assert_has_calls([
            call('email/export_posts.txt', user=self.user),
            call('email/export_posts.html', user=self.user),
        ], any_order=True)
        _, kwargs = self.mock_send_email.call_args
        self.assertEqual(kwargs['text_body'], '<email/export_posts.txt>')
        self.assertEqual(kwargs['html_body'], '<email/export_posts.html>')

    def test_export_reports_progress_in_order(self):
        tasks.export_posts(user_id=1)

        # 0 al inicio; 33, 66, 100 por cada post; 100 final (finally)
        self.assertEqual(self.mock_progress.call_args_list,
                         [call(0), call(33), call(66), call(100), call(100)])

    def test_export_sleeps_once_per_post(self):
        tasks.export_posts(user_id=1)

        self.assertEqual(self.mock_time.sleep.call_count, 3)
        self.mock_time.sleep.assert_called_with(5)

    def test_export_user_without_posts_sends_empty_attachment(self):
        self.mock_db.session.scalar.return_value = 0
        self.mock_db.session.scalars.return_value = iter([])

        tasks.export_posts(user_id=1)  # no debe lanzar ZeroDivisionError

        _, kwargs = self.mock_send_email.call_args
        self.assertEqual(json.loads(kwargs['attachments'][0][2]),
                         {'posts': []})
        self.mock_time.sleep.assert_not_called()

    def test_export_failure_in_email_is_logged_and_task_is_closed(self):
        self.mock_send_email.side_effect = RuntimeError('SMTP down')

        with patch.object(tasks.app, 'logger') as mock_logger:
            tasks.export_posts(user_id=1)  # la excepcion se captura

        mock_logger.error.assert_called_once()
        self.assertEqual(mock_logger.error.call_args[0][0],
                         'Unhandled exception')
        # progreso forzado a 100 en el except y en el finally
        self.assertEqual(self.mock_progress.call_args_list[-2:],
                         [call(100), call(100)])

    def test_export_failure_when_user_not_found(self):
        self.mock_db.session.get.return_value = None  # repositorio sin datos

        with patch.object(tasks.app, 'logger') as mock_logger:
            tasks.export_posts(user_id=999)

        self.mock_send_email.assert_not_called()
        mock_logger.error.assert_called_once()
        self.mock_progress.assert_called_with(100)


class SetTaskProgressUnitTest(AppContextTestCase):
    """_set_task_progress con rq job y repositorio doblados."""

    def setUp(self):
        super().setUp()
        self.job = MagicMock(name='job')
        self.job.meta = {}
        self.job.get_id.return_value = 'job-1'
        self.task = MagicMock(name='task', complete=False)
        self.db_patcher = patch.object(tasks, 'db')
        self.mock_db = self.db_patcher.start()
        self.mock_db.session.get.return_value = self.task

    def tearDown(self):
        patch.stopall()
        super().tearDown()

    def test_without_job_does_nothing(self):
        with patch.object(tasks, 'get_current_job', return_value=None):
            tasks._set_task_progress(50)

        self.mock_db.session.get.assert_not_called()
        self.mock_db.session.commit.assert_not_called()

    def test_with_job_stores_progress_and_notifies_user(self):
        with patch.object(tasks, 'get_current_job', return_value=self.job):
            tasks._set_task_progress(40)

        self.assertEqual(self.job.meta['progress'], 40)
        self.job.save_meta.assert_called_once_with()
        self.mock_db.session.get.assert_called_once_with(tasks.Task, 'job-1')
        self.task.user.add_notification.assert_called_once_with(
            'task_progress', {'task_id': 'job-1', 'progress': 40})
        self.mock_db.session.commit.assert_called_once_with()
        self.assertFalse(self.task.complete)  # < 100: sigue incompleta

    def test_progress_100_marks_task_complete(self):
        with patch.object(tasks, 'get_current_job', return_value=self.job):
            tasks._set_task_progress(100)

        self.assertTrue(self.task.complete)
        self.mock_db.session.commit.assert_called_once_with()


# ===========================================================================
# 1b. Integracion: servicio real + BD SQLite en memoria; solo se dobla rq
# ===========================================================================
class SetTaskProgressIntegrationTest(AppContextTestCase):
    def setUp(self):
        super().setUp()
        db.create_all()
        self.user = User(username='lu', email='lu@example.com')
        db.session.add(self.user)
        db.session.add(Task(id='job-1', name='export_posts',
                            description='Exporting posts...', user=self.user))
        db.session.commit()
        self.job = MagicMock()
        self.job.meta = {}
        self.job.get_id.return_value = 'job-1'

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        super().tearDown()

    def test_progress_is_persisted_and_notification_created(self):
        with patch.object(tasks, 'get_current_job', return_value=self.job):
            tasks._set_task_progress(60)

        task = db.session.get(Task, 'job-1')
        self.assertFalse(task.complete)
        notifications = db.session.scalars(
            self.user.notifications.select()).all()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].name, 'task_progress')
        self.assertEqual(notifications[0].get_data(),
                         {'task_id': 'job-1', 'progress': 60})

    def test_progress_100_persists_complete_flag(self):
        with patch.object(tasks, 'get_current_job', return_value=self.job):
            tasks._set_task_progress(100)

        self.assertTrue(db.session.get(Task, 'job-1').complete)


# ===========================================================================
# 2. translate.translate  (requests.post doblado)
# ===========================================================================
class TranslateServiceTest(AppContextTestCase):
    def _response(self, status=200, body=None):
        resp = MagicMock(status_code=status)
        resp.json.return_value = body
        return resp

    @patch('app.translate.requests.post')
    def test_returns_translated_text_on_success(self, mock_post):
        mock_post.return_value = self._response(
            200, [{'translations': [{'text': 'Hola mundo'}]}])

        result = _translate('Hello world', 'en', 'es')

        self.assertEqual(result, 'Hola mundo')

    @patch('app.translate.requests.post')
    def test_calls_api_with_expected_url_headers_and_body(self, mock_post):
        mock_post.return_value = self._response(
            200, [{'translations': [{'text': 'x'}]}])

        _translate('Hello', 'en', 'es')

        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        self.assertIn('api.cognitive.microsofttranslator.com/translate', url)
        self.assertIn('from=en', url)
        self.assertIn('to=es', url)
        self.assertEqual(
            mock_post.call_args.kwargs['headers']['Ocp-Apim-Subscription-Key'],
            'fake-key')
        self.assertEqual(mock_post.call_args.kwargs['json'],
                         [{'Text': 'Hello'}])

    @patch('app.translate.requests.post')
    def test_returns_error_message_when_api_fails(self, mock_post):
        mock_post.return_value = self._response(500)

        result = _translate('Hello', 'en', 'es')

        self.assertEqual(result, 'Error: the translation service failed.')

    @patch('app.translate.requests.post')
    def test_returns_error_when_unauthorized(self, mock_post):
        mock_post.return_value = self._response(401)

        self.assertIn('failed', _translate('Hello', 'en', 'es'))

    @patch('app.translate.requests.post')
    def test_not_configured_when_key_is_empty_does_not_call_api(self, mock_post):
        with patch.dict(self.app.config, {'MS_TRANSLATOR_KEY': ''}):
            result = _translate('Hello', 'en', 'es')

        self.assertEqual(result,
                         'Error: the translation service is not configured.')
        mock_post.assert_not_called()

    @patch('app.translate.requests.post')
    def test_not_configured_when_key_missing(self, mock_post):
        cfg = dict(self.app.config)
        cfg.pop('MS_TRANSLATOR_KEY')
        with patch.object(self.app, 'config', cfg):
            result = _translate('Hello', 'en', 'es')

        self.assertIn('not configured', result)
        mock_post.assert_not_called()


def _translate(*args):
    from app.translate import translate
    return translate(*args)


# ===========================================================================
# 3. email.send_email / send_async_email  (mail y Thread doblados)
# ===========================================================================
class SendEmailServiceTest(AppContextTestCase):
    @patch('app.email.Thread')
    @patch('app.email.mail')
    def test_sync_sends_immediately_without_thread(self, mock_mail, mock_thread):
        from app.email import send_email

        send_email('Subject', 'from@example.com', ['to@example.com'],
                   'text', '<p>html</p>', sync=True)

        mock_mail.send.assert_called_once()
        msg = mock_mail.send.call_args[0][0]
        self.assertEqual(msg.subject, 'Subject')
        self.assertEqual(msg.sender, 'from@example.com')
        self.assertEqual(msg.recipients, ['to@example.com'])
        self.assertEqual(msg.body, 'text')
        self.assertEqual(msg.html, '<p>html</p>')
        mock_thread.assert_not_called()

    @patch('app.email.Thread')
    @patch('app.email.mail')
    def test_async_starts_thread_and_does_not_send_directly(
            self, mock_mail, mock_thread):
        from app.email import send_email, send_async_email

        send_email('S', 'a@example.com', ['b@example.com'], 't', 'h')

        mock_thread.assert_called_once()
        self.assertIs(mock_thread.call_args.kwargs['target'], send_async_email)
        mock_thread.return_value.start.assert_called_once_with()
        mock_mail.send.assert_not_called()

    @patch('app.email.mail')
    def test_attachments_are_added_to_message(self, mock_mail):
        from app.email import send_email

        send_email('S', 'a@example.com', ['b@example.com'], 't', 'h',
                   attachments=[('posts.json', 'application/json', '{}')],
                   sync=True)

        msg = mock_mail.send.call_args[0][0]
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.attachments[0].filename, 'posts.json')
        self.assertEqual(msg.attachments[0].content_type, 'application/json')

    @patch('app.email.mail')
    def test_no_attachments_by_default(self, mock_mail):
        from app.email import send_email

        send_email('S', 'a@example.com', ['b@example.com'], 't', 'h', sync=True)

        self.assertEqual(mock_mail.send.call_args[0][0].attachments, [])

    @patch('app.email.mail')
    def test_send_async_email_uses_app_context(self, mock_mail):
        from app.email import send_async_email
        fake_app = MagicMock()
        msg = MagicMock()

        send_async_email(fake_app, msg)

        fake_app.app_context.assert_called_once_with()
        fake_app.app_context.return_value.__enter__.assert_called_once()
        mock_mail.send.assert_called_once_with(msg)


# ===========================================================================
# 4. auth.email.send_password_reset_email  (send_email y User doblados)
# ===========================================================================
class PasswordResetEmailServiceTest(AppContextTestCase):
    def setUp(self):
        super().setUp()
        self.user = MagicMock(email='lu@example.com')
        self.user.get_reset_password_token.return_value = 'tok-123'

    @patch('app.auth.email.render_template', return_value='rendered')
    @patch('app.auth.email.send_email')
    def test_sends_email_to_user_with_admin_as_sender(self, mock_send, _r):
        from app.auth.email import send_password_reset_email

        send_password_reset_email(self.user)

        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertIn('Reset Your Password', args[0])
        self.assertEqual(kwargs['sender'], 'admin@example.com')
        self.assertEqual(kwargs['recipients'], ['lu@example.com'])
        self.assertEqual(kwargs['text_body'], 'rendered')
        self.assertEqual(kwargs['html_body'], 'rendered')

    @patch('app.auth.email.render_template', return_value='rendered')
    @patch('app.auth.email.send_email')
    def test_requests_token_once_and_passes_it_to_templates(
            self, mock_send, mock_render):
        from app.auth.email import send_password_reset_email

        send_password_reset_email(self.user)

        self.user.get_reset_password_token.assert_called_once_with()
        mock_render.assert_has_calls([
            call('email/reset_password.txt', user=self.user, token='tok-123'),
            call('email/reset_password.html', user=self.user, token='tok-123'),
        ])

    @patch('app.auth.email.render_template', return_value='rendered')
    @patch('app.auth.email.send_email',
           side_effect=ConnectionError('SMTP down'))
    def test_propagates_errors_from_email_service(self, _s, _r):
        from app.auth.email import send_password_reset_email

        with self.assertRaises(ConnectionError):
            send_password_reset_email(self.user)


# ===========================================================================
# 5. search (Elasticsearch doblado)
# ===========================================================================
class SearchServiceTest(AppContextTestCase):
    def setUp(self):
        super().setUp()
        self.es = MagicMock(name='elasticsearch')
        self.app.elasticsearch = self.es
        self.model = MagicMock(id=5, body='hola', __searchable__=['body'])

    def tearDown(self):
        self.app.elasticsearch = None
        super().tearDown()

    def test_add_to_index_indexes_searchable_fields(self):
        from app.search import add_to_index

        add_to_index('post', self.model)

        self.es.index.assert_called_once_with(
            index='post', id=5, document={'body': 'hola'})

    def test_remove_from_index_deletes_by_id(self):
        from app.search import remove_from_index

        remove_from_index('post', self.model)

        self.es.delete.assert_called_once_with(index='post', id=5)

    def test_query_index_returns_ids_and_total(self):
        from app.search import query_index
        self.es.search.return_value = {
            'hits': {'total': {'value': 2},
                     'hits': [{'_id': '10'}, {'_id': '3'}]}}

        ids, total = query_index('post', 'hola', page=2, per_page=10)

        self.assertEqual((ids, total), ([10, 3], 2))
        kwargs = self.es.search.call_args.kwargs
        self.assertEqual(kwargs['index'], 'post')
        self.assertEqual(kwargs['from_'], 10)  # (page-1)*per_page
        self.assertEqual(kwargs['size'], 10)

    def test_query_index_without_results(self):
        from app.search import query_index
        self.es.search.return_value = {
            'hits': {'total': {'value': 0}, 'hits': []}}

        self.assertEqual(query_index('post', 'nada', 1, 10), ([], 0))

    def test_all_operations_are_noop_when_elasticsearch_disabled(self):
        from app.search import add_to_index, remove_from_index, query_index
        self.app.elasticsearch = None

        add_to_index('post', self.model)
        remove_from_index('post', self.model)
        result = query_index('post', 'x', 1, 10)

        self.assertEqual(result, ([], 0))
        self.es.index.assert_not_called()
        self.es.delete.assert_not_called()
        self.es.search.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
