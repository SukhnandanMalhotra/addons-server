import base64
import json
import os
import tempfile

from mock import patch
from nose.tools import eq_

import amo
from addons.models import (Addon, AddonUpsell, AddonCategory, AddonDeviceType,
                           AddonUser, Category, Preview)
from amo.tests import app_factory, AMOPaths
from files.models import FileUpload
from users.models import UserProfile

import mkt
from mkt.api.tests.test_oauth import BaseOAuth
from mkt.api.base import get_url
from mkt.constants import APP_IMAGE_SIZES
from mkt.site.fixtures import fixture
from mkt.webapps.models import ContentRating, ImageAsset, Webapp
from mkt.zadmin.models import FeaturedApp, FeaturedAppRegion
from reviews.models import Review


class ValidationHandler(BaseOAuth):
    fixtures = fixture('user_2519', 'user_admin')

    def setUp(self):
        super(ValidationHandler, self).setUp()
        self.list_url = ('api_dispatch_list', {'resource_name': 'validation'})
        self.get_url = None
        self.user = UserProfile.objects.get(pk=2519)

    def test_has_cors(self):
        res = self.client.get(self.list_url)
        eq_(res['Access-Control-Allow-Origin'], '*')
        eq_(res['Access-Control-Allow-Methods'], 'POST, OPTIONS')

    def create(self):
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest':
                                                'http://foo.com'}))
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'validation',
                         'pk': json.loads(res.content)['id']})
        return res

    def get(self):
        return FileUpload.objects.all()[0]

    def get_error(self, response):
        return json.loads(response.content)['error_message']


class TestAddValidationHandler(ValidationHandler):

    def test_verbs(self):
        self._allowed_verbs(self.list_url, ['post'])

    def test_good(self):
        res = self.create()
        eq_(res.status_code, 201)  # Note! This should be a 202.
        content = json.loads(res.content)
        eq_(content['processed'], True)
        obj = FileUpload.objects.get(uuid=content['id'])
        eq_(obj.user, self.user)

    @patch('mkt.api.resources.tasks.fetch_manifest')
    def test_fetch(self, fetch):
        self.create()
        assert fetch.called

    def test_missing(self):
        res = self.client.post(self.list_url, data=json.dumps({}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['manifest'], ['This field is required.'])

    def test_bad(self):
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': 'blurgh'}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['manifest'], ['Enter a valid URL.'])

    def test_anon(self):
        res = self.anon.post(self.list_url,
                             data=json.dumps({'manifest':
                                              'http://foo.com'}))
        eq_(res.status_code, 201)


class TestPackagedValidation(amo.tests.AMOPaths, ValidationHandler):

    def setUp(self):
        super(TestPackagedValidation, self).setUp()
        name = 'mozball.zip'
        path = self.packaged_app_path(name)
        self.file = base64.b64encode(open(path).read())
        self.data = {'data': self.file, 'name': name,
                     'type': 'application/zip'}

    def create(self):
        res = self.client.post(self.list_url,
                               data=json.dumps({'upload': self.data}))
        if res.status_code < 400:
            self.get_url = ('api_dispatch_detail',
                            {'resource_name': 'validation',
                             'pk': json.loads(res.content)['id']})

        return res

    def test_good(self):
        res = self.create()
        eq_(res.status_code, 201)  # Note! This should be a 202.
        content = json.loads(res.content)
        eq_(content['processed'], True)
        obj = FileUpload.objects.get(uuid=content['id'])
        eq_(obj.user, self.user)

    @patch('mkt.developers.forms.MAX_PACKAGED_APP_SIZE', 1)
    def test_too_big(self):
        res = self.create()
        eq_(res.status_code, 400)
        obj = FileUpload.objects.get()
        messages = json.loads(obj.validation)['messages']
        eq_(messages[0]['message'],
            ["Packaged app too large for submission.",
             "Packages must be less than 1 byte."])

    def form_errors(self, data, errors):
        self.data = data
        res = self.create()
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['upload'], errors)

    def test_missing(self):
        self.form_errors({'data': self.file, 'name': 'mozball.zip'},
                         [u'Type and data are required.'])

    def test_missing_name(self):
        self.form_errors({'data': self.file, 'type': 'application/zip'},
                         [u'Name not specified.'])

    def test_wrong(self):
        self.form_errors({'data': self.file, 'name': 'mozball.zip',
                          'type': 'application/foo'},
                         [u'Type must be application/zip.'])

    def test_invalid(self):
        self.form_errors({'data': 'x', 'name': 'mozball.zip',
                          'type': 'application/foo'},
                         [u'File must be base64 encoded.'])


class TestGetValidationHandler(ValidationHandler):

    def create(self):
        res = FileUpload.objects.create(user=self.user, path='http://foo.com')
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'validation', 'pk': res.pk})
        return res

    def test_verbs(self):
        self.create()
        self._allowed_verbs(self.get_url, ['get'])

    def test_check(self):
        self.create()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)

    def test_anon(self):
        self.create()
        res = self.anon.get(self.get_url)
        eq_(res.status_code, 200)

    def test_not_found(self):
        url = ('api_dispatch_detail',
                {'resource_name': 'validation', 'pk': '123123123'})
        res = self.client.get(url)
        eq_(res.status_code, 404)

    def test_not_run(self):
        self.create()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        eq_(json.loads(res.content)['processed'], False)

    def test_pass(self):
        obj = self.create()
        obj.update(valid=True)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['processed'], True)
        eq_(data['valid'], True)

    def test_failure(self):
        obj = self.create()
        error = '{"errors": 1, "messages": [{"tier": 1, "message": "nope"}]}'
        obj.update(valid=False, validation=error)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['processed'], True)
        eq_(data['valid'], False)
        eq_(data['validation'], json.loads(error))


class CreateHandler(BaseOAuth):
    fixtures = fixture('user_2519', 'platform_all')

    def setUp(self):
        super(CreateHandler, self).setUp()
        self.list_url = ('api_dispatch_list', {'resource_name': 'app'})
        self.user = UserProfile.objects.get(pk=2519)
        self.file = tempfile.NamedTemporaryFile('w', suffix='.webapp').name
        self.manifest_copy_over(self.file, 'mozball-nice-slug.webapp')
        self.categories = []
        for x in range(0, 2):
            self.categories.append(Category.objects.create(
                name='cat-%s' % x,
                type=amo.ADDON_WEBAPP))

    def create(self):
        return FileUpload.objects.create(user=self.user, path=self.file,
                                         name=self.file, valid=True)


def _mock_fetch_content(url):
    return open(os.path.join(os.path.dirname(__file__),
                             '..', '..', 'developers', 'tests', 'icons',
                             '337141-128.png'))


class TestAppCreateHandler(CreateHandler, AMOPaths):
    fixtures = fixture('app_firefox', 'platform_all', 'user_admin',
                       'user_2519', 'user_999')

    def count(self):
        return Addon.objects.count()

    def test_verbs(self):
        self.create()
        self._allowed_verbs(self.list_url, ['get', 'post'])
        self.create_app()
        self._allowed_verbs(self.get_url, ['get', 'put'])

    def test_not_accepted_tos(self):
        self.user.update(read_dev_agreement=None)
        obj = self.create()
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 401)

    def test_not_valid(self):
        obj = self.create()
        obj.update(valid=False)
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['__all__'], ['Upload not valid.'])
        eq_(self.count(), 0)

    def test_not_there(self):
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest':
                                   'some-random-32-character-stringy'}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['__all__'], ['No upload found.'])
        eq_(self.count(), 0)

    def test_anon(self):
        obj = self.create()
        obj.update(user=None)
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 403)
        eq_(self.count(), 0)

    def test_not_yours(self):
        obj = self.create()
        obj.update(user=UserProfile.objects.get(email='admin@mozilla.com'))
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 403)
        eq_(self.count(), 0)

    @patch('mkt.api.resources.record_action')
    def test_create(self, record_action):
        obj = self.create()
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': obj.uuid}))
        eq_(res.status_code, 201)
        content = json.loads(res.content)
        eq_(content['status'], 0)
        eq_(content['slug'], u'mozillaball')
        eq_(content['support_email'], None)
        eq_(self.count(), 1)

        app = Webapp.objects.get(app_slug=content['slug'])
        eq_(set(app.authors.all()), set([self.user]))
        assert record_action.called

    def create_app(self):
        obj = self.create()
        res = self.client.post(self.list_url,
                               data=json.dumps({'manifest': obj.uuid}))
        pk = json.loads(res.content)['id']
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'app', 'pk': pk})
        return Webapp.objects.get(pk=pk)

    @patch('mkt.developers.tasks._fetch_content', _mock_fetch_content)
    def test_imageassets(self):
        asset_count = ImageAsset.objects.count()
        app = self.create_app()
        eq_(ImageAsset.objects.count() - len(APP_IMAGE_SIZES), asset_count)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(len(data['image_assets']), len(APP_IMAGE_SIZES))
        self.assertSetEqual(data['image_assets'].keys(),
                            [i['slug'] for i in APP_IMAGE_SIZES])
        self.assertSetEqual(map(tuple, data['image_assets'].values()),
                            [(app.get_image_asset_url(i['slug']),
                              app.get_image_asset_hue(i['slug']))
                            for i in APP_IMAGE_SIZES])

    def test_upsell(self):
        app = self.create_app()
        upsell = app_factory()
        AddonUpsell.objects.create(free=app, premium=upsell)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        content = json.loads(res.content)
        eq_(len(content.get('upsell', {})), len(content))

    def test_get(self):
        self.create_app()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        content = json.loads(res.content)
        eq_(content['status'], 0)

    def test_get_slug(self):
        app = self.create_app()
        url = ('api_dispatch_detail',
               {'resource_name': 'app', 'app_slug': app.app_slug})
        res = self.client.get(url)
        content = json.loads(res.content)
        eq_(content['id'], str(app.pk))

    def test_list(self):
        app = self.create_app()
        res = self.client.get(self.list_url)
        eq_(res.status_code, 200)
        content = json.loads(res.content)
        eq_(content['meta']['total_count'], 1)
        eq_(content['objects'][0]['id'], str(app.pk))

    def test_list_anon(self):
        eq_(self.anon.get(self.list_url).status_code, 403)

    def test_get_device(self):
        app = self.create_app()
        AddonDeviceType.objects.create(addon=app,
                                       device_type=amo.DEVICE_DESKTOP.id)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        content = json.loads(res.content)
        eq_(content['device_types'], [u'desktop'])

    def test_not_public(self):
        self.create_app()
        res = self.anon.get(self.get_url)
        eq_(res.status_code, 403)

    def test_get_public(self):
        app = self.create_app()
        app.update(status=amo.STATUS_PUBLIC)
        res = self.anon.get(self.get_url)
        eq_(res.status_code, 200)

    def test_get_previews(self):
        app = self.create_app()
        res = self.client.get(self.get_url)
        eq_(len(json.loads(res.content)['previews']), 0)
        Preview.objects.create(addon=app)
        res = self.client.get(self.get_url)
        eq_(len(json.loads(res.content)['previews']), 1)

    def test_get_not_mine(self):
        obj = self.create_app()
        obj.authors.clear()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 403)

    def base_data(self):
        return {'support_email': 'a@a.com',
                'privacy_policy': 'wat',
                'homepage': 'http://www.whatever.com',
                'name': 'mozball',
                'categories': [c.pk for c in self.categories],
                'summary': 'wat...',
                'device_types': amo.DEVICE_TYPES.keys()}

    def test_put(self):
        app = self.create_app()
        res = self.client.put(self.get_url, data=json.dumps(self.base_data()))
        eq_(res.status_code, 202)
        app = Webapp.objects.get(pk=app.pk)
        eq_(app.privacy_policy, 'wat')

    def test_put_as_post(self):
        # This is really a test of the HTTP_X_HTTP_METHOD_OVERRIDE header
        # and that signing works correctly. Do a POST, but ask tastypie to do
        # a PUT.
        self.create_app()
        res = self.client.post(self.get_url, data=json.dumps(self.base_data()),
                               HTTP_X_HTTP_METHOD_OVERRIDE='PUT')
        eq_(res.status_code, 202)

    def test_put_anon(self):
        app = self.create_app()
        app.update(status=amo.STATUS_PUBLIC)
        res = self.anon.put(self.get_url, data=json.dumps(self.base_data()))
        eq_(res.status_code, 403)

    def test_put_categories_worked(self):
        app = self.create_app()
        res = self.client.put(self.get_url, data=json.dumps(self.base_data()))
        eq_(res.status_code, 202)
        app = Webapp.objects.get(pk=app.pk)
        eq_(set([c.pk for c in app.categories.all()]),
            set([c.pk for c in self.categories]))

    def test_get_content_ratings(self):
        app = self.create_app()
        ContentRating.objects.create(addon=app, ratings_body=0, rating=2)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        cr = data.get('content_ratings')
        self.assertIn('DJCTQ', cr.keys())
        eq_(cr.get('DJCTQ')['name'], u'12')
        self.assertIn('description', cr.get('DJCTQ'))

    def test_dehydrate(self):
        app = self.create_app()
        res = self.client.put(self.get_url, data=json.dumps(self.base_data()))
        eq_(res.status_code, 202)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        self.assertSetEqual(data['categories'],
                            [c.pk for c in self.categories])
        eq_(data['current_version']['version'], u'1.0')
        eq_(data['current_version']['release_notes'], None)
        self.assertSetEqual(data['device_types'],
                            [n.api_name for n in amo.DEVICE_TYPES.values()])
        eq_(data['homepage'], u'http://www.whatever.com')
        eq_(data['is_packaged'], False)
        eq_(data['listed_authors'][0].get('name'), self.user.display_name)
        eq_(data['manifest_url'], app.manifest_url)
        eq_(data['premium_type'], 'free')
        eq_(data['price'], u'Free')
        eq_(data['public_stats'], False)
        eq_(data['support_email'], u'a@a.com')
        eq_(data['ratings'], {'count': 0, 'average': 0.0})
        eq_(data['user'], {'owns': True})

    def test_ratings(self):
        app = self.create_app()
        rater = UserProfile.objects.get(pk=999)
        Review.objects.create(addon=app, user=self.user, body='yes', rating=3)
        Review.objects.create(addon=app, user=rater, body='no', rating=2)
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['ratings'], {'count': 2, 'average': 2.5})

    def test_put_wrong_category(self):
        self.create_app()
        wrong = Category.objects.create(name='wrong', type=amo.ADDON_EXTENSION,
                                        application_id=amo.FIREFOX.id)
        data = self.base_data()
        data['categories'] = [wrong.pk]
        res = self.client.put(self.get_url, data=json.dumps(data))
        eq_(res.status_code, 400)
        assert 'Select a valid choice' in self.get_error(res)['categories'][0]

    def test_put_no_categories(self):
        self.create_app()
        data = self.base_data()
        del data['categories']
        res = self.client.put(self.get_url, data=json.dumps(data))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['categories'], ['This field is required.'])

    def test_put_no_desktop(self):
        self.create_app()
        data = self.base_data()
        del data['device_types']
        res = self.client.put(self.get_url, data=json.dumps(data))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['device_types'], ['This field is required.'])

    def test_put_devices_worked(self):
        app = self.create_app()
        data = self.base_data()
        data['device_types'] = [a.api_name for a in amo.DEVICE_TYPES.values()]
        res = self.client.put(self.get_url, data=json.dumps(data))
        eq_(res.status_code, 202)
        app = Webapp.objects.get(pk=app.pk)
        eq_(set(d for d in app.device_types),
            set(amo.DEVICE_TYPES[d] for d in amo.DEVICE_TYPES.keys()))

    def test_put_desktop_error_nice(self):
        self.create_app()
        data = self.base_data()
        data['device_types'] = [12345]
        res = self.client.put(self.get_url, data=json.dumps(data))
        eq_(res.status_code, 400)
        assert '12345' in self.get_error(res)['device_types'][0], (
            self.get_error(res))

    def test_put_not_mine(self):
        obj = self.create_app()
        obj.authors.clear()
        res = self.client.put(self.get_url, data='{}')
        eq_(res.status_code, 403)

    def test_put_not_there(self):
        url = ('api_dispatch_detail', {'resource_name': 'app', 'pk': 123})
        res = self.client.put(url, data='{}')
        eq_(res.status_code, 404)


class CreatePackagedHandler(amo.tests.AMOPaths, BaseOAuth):
    fixtures = fixture('user_2519', 'platform_all')

    def setUp(self):
        super(CreatePackagedHandler, self).setUp()
        self.list_url = ('api_dispatch_list', {'resource_name': 'app'})
        self.user = UserProfile.objects.get(pk=2519)
        self.file = tempfile.NamedTemporaryFile('w', suffix='.zip').name
        self.packaged_copy_over(self.file, 'mozball.zip')
        self.categories = []
        for x in range(0, 2):
            self.categories.append(Category.objects.create(
                name='cat-%s' % x,
                type=amo.ADDON_WEBAPP))

    def create(self):
        return FileUpload.objects.create(user=self.user, path=self.file,
                                         name=self.file, valid=True)


class TestPackagedAppCreateHandler(CreatePackagedHandler):
    fixtures = fixture('user_2519', 'platform_all')

    def test_create(self):
        obj = self.create()
        res = self.client.post(self.list_url,
                               data=json.dumps({'upload': obj.uuid}))
        eq_(res.status_code, 201)
        content = json.loads(res.content)
        eq_(content['status'], 0)

        # Note the packaged status is not returned in the result.
        app = Webapp.objects.get(app_slug=content['slug'])
        eq_(app.is_packaged, True)


class TestListHandler(CreateHandler, AMOPaths):
    fixtures = fixture('user_2519', 'user_999', 'platform_all')

    def create(self, users):
        app = Addon.objects.create(type=amo.ADDON_WEBAPP)
        for user in users:
            AddonUser.objects.create(user=user, addon=app)
        return app

    def create_apps(self, *all_owners):
        apps = []
        for owners in all_owners:
            owners = [UserProfile.objects.get(pk=pk) for pk in owners]
            apps.append(self.create(owners))

        return apps

    def test_create(self):
        apps = self.create_apps([2519], [999])
        res = self.client.get(self.list_url)
        data = json.loads(res.content)
        eq_(data['meta']['total_count'], 1)
        eq_(data['objects'][0]['id'], str(apps[0].pk))

    def test_multiple(self):
        apps = self.create_apps([2519], [999, 2519])
        res = self.client.get(self.list_url)
        data = json.loads(res.content)
        eq_(data['meta']['total_count'], 2)
        pks = set([data['objects'][0]['id'], data['objects'][1]['id']])
        eq_(pks, set([str(app.pk) for app in apps]))

    def test_lang(self):
        app = app_factory(summary={'fr': 'Le blah', 'en-US': 'Blah'})
        url = get_url('app', app.pk)

        res = self.client.get(url, HTTP_ACCEPT_LANGUAGE='en-US')
        eq_(json.loads(res.content)['summary'], 'Blah')

        res = self.client.get(url, HTTP_ACCEPT_LANGUAGE='fr')
        eq_(json.loads(res.content)['summary'], 'Le blah')


class TestAppStatusHandler(CreateHandler, AMOPaths):
    fixtures = fixture('user_2519', 'platform_all')

    def setUp(self):
        super(TestAppStatusHandler, self).setUp()
        self.list_url = ('api_dispatch_list', {'resource_name': 'status'})

    def create_app(self):
        obj = self.create()
        res = self.client.post(('api_dispatch_list', {'resource_name': 'app'}),
                               data=json.dumps({'manifest': obj.uuid}))
        pk = json.loads(res.content)['id']
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'status', 'pk': pk})
        return Webapp.objects.get(pk=pk)

    def test_verbs(self):
        self._allowed_verbs(self.list_url, [])

    def test_has_no_cors(self):
        res = self.client.get(self.list_url)
        assert 'access-control-allow-origin' not in res

    def test_status(self):
        self.create_app()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)
        data = json.loads(res.content)
        eq_(data['disabled_by_user'], False)
        eq_(data['status'], 'incomplete')

    def test_disable(self):
        app = self.create_app()
        res = self.client.patch(self.get_url,
                                data=json.dumps({'disabled_by_user': True}))
        eq_(res.status_code, 202, res.content)
        app = app.__class__.objects.get(pk=app.pk)
        eq_(app.disabled_by_user, True)
        eq_(app.status, amo.STATUS_NULL)

    def test_change_status_fails(self):
        self.create_app()
        res = self.client.patch(self.get_url,
                        data=json.dumps({'status': 'pending'}))
        eq_(res.status_code, 400)
        assert isinstance(self.get_error(res)['status'], list)

    @patch('mkt.webapps.models.Webapp.is_complete')
    def test_change_status_passes(self, is_complete):
        is_complete.return_value = True, []
        app = self.create_app()
        res = self.client.patch(self.get_url,
                        data=json.dumps({'status': 'pending'}))
        eq_(res.status_code, 202, res.content)
        eq_(app.__class__.objects.get(pk=app.pk).status, amo.STATUS_PENDING)

    @patch('mkt.webapps.models.Webapp.is_complete')
    def test_cant_skip(self, is_complete):
        is_complete.return_value = True, []
        app = self.create_app()
        res = self.client.patch(self.get_url,
                        data=json.dumps({'status': 'public'}))
        eq_(res.status_code, 400)
        assert 'available choices' in self.get_error(res)['status'][0]
        eq_(Addon.objects.get(pk=app.pk).status, amo.STATUS_NULL)

    def test_public_waiting(self):
        app = self.create_app()
        app.update(status=amo.STATUS_PUBLIC_WAITING)
        res = self.client.patch(self.get_url,
                        data=json.dumps({'status': 'public'}))
        eq_(res.status_code, 202)
        eq_(app.__class__.objects.get(pk=app.pk).status, amo.STATUS_PUBLIC)


class TestAppDetail(BaseOAuth, AMOPaths):
    fixtures = fixture('user_2519', 'webapp_337141')

    def test_price(self):
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'app', 'pk': 337141})
        res = self.client.get(self.get_url)
        data = json.loads(res.content)
        eq_(data['price'], None)


class TestCategoryHandler(BaseOAuth):

    def setUp(self):
        super(TestCategoryHandler, self).setUp()
        self.cat = Category.objects.create(name='Webapp',
                                           type=amo.ADDON_WEBAPP,
                                           slug='thewebapp')
        self.cat.name = {'fr': 'Le Webapp'}
        self.cat.save()
        self.other = Category.objects.create(name='other',
                                             type=amo.ADDON_EXTENSION)

        self.list_url = ('api_dispatch_list', {'resource_name': 'category'})
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'category', 'pk': self.cat.pk})

    def test_verbs(self):
        self._allowed_verbs(self.list_url, ['get'])
        self._allowed_verbs(self.get_url, ['get'])

    def test_has_cors(self):
        res = self.client.get(self.list_url)
        eq_(res['Access-Control-Allow-Origin'], '*')
        eq_(res['Access-Control-Allow-Methods'], 'GET, OPTIONS')

    def test_weight(self):
        self.cat.update(weight=-1)
        res = self.anon.get(self.list_url)
        data = json.loads(res.content)
        eq_(data['meta']['total_count'], 0)

    def test_get_slug(self):
        url = ('api_dispatch_detail',
               {'resource_name': 'category', 'slug': self.cat.slug})
        res = self.client.get(url)
        data = json.loads(res.content)
        eq_(data['id'], str(self.cat.pk))

    def test_get_categories(self):
        res = self.anon.get(self.list_url)
        data = json.loads(res.content)
        eq_(data['meta']['total_count'], 1)
        eq_(data['objects'][0]['name'], 'Webapp')
        eq_(data['objects'][0]['slug'], 'thewebapp')

    def test_get_category(self):
        res = self.anon.get(self.get_url)
        data = json.loads(res.content)
        eq_(data['name'], 'Webapp')

    def test_get_category_localised(self):
        res = self.anon.get(self.get_url, HTTP_ACCEPT_LANGUAGE='fr')
        data = json.loads(res.content)
        eq_(data['name'], 'Le Webapp')

    def test_get_other_category(self):
        res = self.anon.get(('api_dispatch_detail',
                             {'resource_name': 'category',
                              'pk': self.other.pk}))
        eq_(res.status_code, 404)


class TestPreviewHandler(BaseOAuth, AMOPaths):
    fixtures = fixture('user_2519', 'webapp_337141')

    def setUp(self):
        super(TestPreviewHandler, self).setUp()
        self.app = Webapp.objects.get(pk=337141)
        self.user = UserProfile.objects.get(pk=2519)
        AddonUser.objects.create(user=self.user, addon=self.app)
        self.file = base64.b64encode(open(self.preview_image(), 'r').read())
        self.list_url = ('api_dispatch_list', {'resource_name': 'preview'},
                         {'app': self.app.pk})
        self.good = {'file': {'data': self.file, 'type': 'image/jpg'},
                     'position': 1}

    def test_has_cors(self):
        res = self.client.get(self.list_url)
        eq_(res['Access-Control-Allow-Origin'], '*')
        eq_(res['Access-Control-Allow-Methods'], 'POST, OPTIONS')

    def test_no_addon(self):
        list_url = ('api_dispatch_list', {'resource_name': 'preview'})
        res = self.client.post(list_url, data=json.dumps(self.good))
        eq_(res.status_code, 404)

    def test_post_preview(self):
        res = self.client.post(self.list_url, data=json.dumps(self.good))
        eq_(res.status_code, 201)
        previews = self.app.previews
        eq_(previews.count(), 1)
        eq_(previews.all()[0].position, 1)

    def test_wrong_url(self):
        url = list(self.list_url)
        url[-1]['app'] = 'booyah'
        res = self.client.post(url, data=json.dumps(self.good))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['app'], [u'Enter a whole number.'])

    def test_not_mine(self):
        self.app.authors.clear()
        res = self.client.post(self.list_url, data=json.dumps(self.good))
        eq_(res.status_code, 403)

    def test_position_missing(self):
        data = {'file': {'data': self.file, 'type': 'image/jpg'}}
        res = self.client.post(self.list_url, data=json.dumps(data))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['position'], ['This field is required.'])

    def test_preview_missing(self):
        res = self.client.post(self.list_url, data=json.dumps({}))
        eq_(res.status_code, 400)
        eq_(self.get_error(res)['position'], ['This field is required.'])

    def create(self):
        self.client.post(self.list_url, data=json.dumps(self.good))
        self.preview = self.app.previews.all()[0]
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'preview', 'pk': self.preview.pk})

    def test_delete(self):
        self.create()
        res = self.client.delete(self.get_url)
        eq_(res.status_code, 204)
        eq_(self.app.previews.count(), 0)

    def test_delete_not_mine(self):
        self.create()
        self.app.authors.clear()
        res = self.client.delete(self.get_url)
        eq_(res.status_code, 403)

    def test_delete_not_there(self):
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'preview', 'pk': 123})
        res = self.client.delete(self.get_url)
        eq_(res.status_code, 404)

    def test_get(self):
        self.create()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 200)

    def test_get_not_mine(self):
        self.create()
        self.app.authors.clear()
        res = self.client.get(self.get_url)
        eq_(res.status_code, 403)

    def test_get_not_there(self):
        self.get_url = ('api_dispatch_detail',
                        {'resource_name': 'preview', 'pk': 123})
        res = self.client.get(self.get_url)
        eq_(res.status_code, 404)


class TestFeaturedHomeHandler(BaseOAuth):

    def setUp(self):
        super(TestFeaturedHomeHandler, self).setUp()
        resource = 'featured/home'
        self.list_url = ('api_dispatch_list', {'resource_name': resource})
        self.get_url = None

        self.cat = Category.objects.create(name='awesome',
                                           type=amo.ADDON_WEBAPP)

        # App, no category, worldwide region.
        self.app1 = Webapp.objects.create(status=amo.STATUS_PUBLIC,
                                          name='App 1')
        f1 = FeaturedApp.objects.create(app=self.app1, category=None)
        FeaturedAppRegion.objects.create(featured_app=f1,
                                         region=mkt.regions.WORLDWIDE.id)

        # App, with category, worldwide region. Mostly to ensure category
        # specific featured apps don't slip into the results.
        self.app2 = Webapp.objects.create(status=amo.STATUS_PUBLIC,
                                          name='App 2')
        AddonCategory.objects.create(category=self.cat, addon=self.app2)
        f2 = FeaturedApp.objects.create(app=self.app2, category=self.cat)
        FeaturedAppRegion.objects.create(featured_app=f2,
                                         region=mkt.regions.WORLDWIDE.id)

        # App, no category, US region.
        self.app3 = Webapp.objects.create(status=amo.STATUS_PUBLIC,
                                          name='App 3')
        f3 = FeaturedApp.objects.create(app=self.app3)
        FeaturedAppRegion.objects.create(featured_app=f3,
                                         region=mkt.regions.US.id)

    def test_verbs(self):
        self._allowed_verbs(self.list_url, ['get'])

    def test_has_cors(self):
        res = self.client.get(self.list_url)
        eq_(res['Access-Control-Allow-Origin'], '*')
        eq_(res['Access-Control-Allow-Methods'], 'GET, OPTIONS')

    def test_get_featured(self):
        res = self.anon.get(self.list_url)
        data = json.loads(res.content)
        eq_(res.status_code, 200)
        eq_(data['objects'][0]['slug'], self.app1.app_slug)

    def test_get_featured_region(self):
        # UK region should come up empty, so we backfill with worldwide.
        res = self.anon.get(self.list_url, data=dict(region='uk'))
        data = json.loads(res.content)
        eq_(res.status_code, 200)
        eq_(data['objects'][0]['slug'], self.app1.app_slug)

        # US region should come have 1 plus worldwide.
        res = self.anon.get(self.list_url, data=dict(region='us'))
        data = json.loads(res.content)
        eq_(res.status_code, 200)
        self.assertSetEqual([o['slug'] for o in data['objects']],
                            ['app-1', 'app-3'])
