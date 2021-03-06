from .generic_views import CreateView
from .generic_views import DeleteView
from .generic_views import GetView
from .generic_views import ListView
from .generic_views import UpdateView
from .helpers import get_object
from flask import abort
from flask import Blueprint
from flask import jsonify
from flask import make_response
from flask import request
from flask_jwt import current_identity
from flask_jwt import jwt_required
from importlib import import_module
from kqueen.auth import encrypt_password
from kqueen.auth.common import generate_auth_options
from kqueen.models import Cluster
from kqueen.models import Organization
from kqueen.models import Provisioner
from kqueen.models import User
from kqueen.config import current_config

import asyncio
import logging
import os
import yaml

config = current_config()
logger = logging.getLogger('kqueen_api')

api = Blueprint('api', __name__)


# error handlers
def error_response(code, error):
    """Return JSONed response of error code.

    Attributes:
        code (int): Error code number.
        error (obj): HTTP error code

    Returns:
        JSONified error response
    """

    response = {'code': code, 'description': error.description}
    return make_response(jsonify(response), code)


@api.errorhandler(400)
def bad_request(error):
    return error_response(400, error)


@api.errorhandler(404)
def not_found(error):
    return error_response(404, error)


@api.errorhandler(500)
def not_implemented(error):
    return error_response(500, error)


@api.route('/')
@api.route('/health')
def index():
    return jsonify({'response': 'Kqueen ready!'})


# Clusters
class ListClusters(ListView):
    object_class = Cluster

    supported_sort_fields = {
        'name': lambda x: (x.name, x.created_at, x.id),
        'provisioner': lambda x: (x.provisioner.name, x.name, x.id),
        'created_at': lambda x: (x.created_at, x.name, x.id),
        'created': lambda x: (x.created_at, x.name, x.id),
        'status': lambda x: (x.state, x.name, x.id),
        'state': lambda x: (x.state, x.name, x.id),
    }

    def filter_objects(self, objects, filters):
        if not filters:
            return objects

        get_cluster_attr = {
            'name': lambda x: x.name,
            'provisioner': lambda x: x.provisioner.name,
        }
        filter_keys = set(filters.keys()).intersection(get_cluster_attr.keys())
        return list(filter(
            lambda x: all(filters[key].lower() in get_cluster_attr[key](x).lower()
                          for key in filter_keys),
            objects
        ))

    def sort_objects(self, objects, key, order):
        return sorted(objects, key=self.supported_sort_fields[key], reverse=order == 'asc')

    async def _update_clusters(self, clusters, loop):
        futures = [
            loop.run_in_executor(
                None,
                cluster.update_state
            )
            for cluster in clusters
        ]
        for result in await asyncio.gather(*futures):
            pass

    def get_content(self, *args, **kwargs):
        clusters = self.obj
        if config.get('CLUSTER_STATE_ON_LIST'):
            try:
                # get or establish event loop
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        raise RuntimeError('Loop already closed')
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                # run coroutines and close loop
                loop.run_until_complete(self._update_clusters(clusters, loop))
                loop.close()
            except Exception as e:
                logger.exception('Asyncio loop is NOT available, fallback to simple looping: ')

                for c in clusters:
                    c.update_state()
                self.obj = clusters

        return super().get_content(self, *args, **kwargs)


class GetClustersHealth(ListView):
    object_class = Cluster

    def get_content(self, *args, **kwargs):
        clusters = self.obj

        deployed_clusters = 0
        healthy_clusters = 0

        for cluster in clusters:
            if cluster.state != config.get('CLUSTER_PROVISIONING_STATE'):
                deployed_clusters += 1
            if cluster.state in [config.get('CLUSTER_UPDATING_STATE'),
                                 config.get('CLUSTER_OK_STATE')]:
                healthy_clusters += 1

        cluster_health = 0
        if healthy_clusters and deployed_clusters:
            cluster_health = int((healthy_clusters / deployed_clusters) * 100)

        return {
            'total': len(clusters),
            'healthy_percentage': cluster_health
        }


class CreateCluster(CreateView):
    object_class = Cluster

    def save_object(self):
        if self.obj.provisioner.state != config.get('PROVISIONER_OK_STATE'):
            msg = 'Cannot create cluster with malfunctioning Provisioner'
            logger.error('Provisioning failed: {}'.format(msg))
            abort(500, description=msg)
        return super().save_object()

    def after_save(self):
        # start provisioning
        prov_status, prov_msg = self.obj.engine.provision()

        if not prov_status:
            logger.error('Provisioning failed: {}'.format(prov_msg))
            self.obj.state = config.get('CLUSTER_ERROR_STATE')
            abort(500, description=prov_msg)


class GetCluster(GetView):
    object_class = Cluster

    def dispatch_request(self, *args, **kwargs):
        self.check_authentication()
        self.set_object(*args, **kwargs)
        self.check_authorization()
        cluster = self.get_content(*args, hide_secure_data=False, **kwargs)
        cluster.update_state()
        cluster = self.hide_secure_data(self.obj)
        return jsonify(cluster)


class UpdateCluster(UpdateView):
    object_class = Cluster


class DeleteCluster(DeleteView):
    object_class = Cluster


api.add_url_rule('/clusters', view_func=ListClusters.as_view('cluster_list'))
api.add_url_rule('/clusters/health', view_func=GetClustersHealth.as_view('clusters_health'))
api.add_url_rule('/clusters', view_func=CreateCluster.as_view('cluster_create'))
api.add_url_rule('/clusters/<uuid:pk>', view_func=GetCluster.as_view('cluster_get'))
api.add_url_rule('/clusters/<uuid:pk>', view_func=UpdateCluster.as_view('cluster_update'))
api.add_url_rule('/clusters/<uuid:pk>', view_func=DeleteCluster.as_view('cluster_delete'))


@api.route('/clusters/<uuid:pk>/status', methods=['GET'])
@jwt_required()
def cluster_status(pk):
    obj = get_object(Cluster, pk, current_identity)

    return jsonify(obj.status())


@api.route('/clusters/<uuid:pk>/topology-data', methods=['GET'])
@jwt_required()
def cluster_topology_data(pk):
    obj = get_object(Cluster, pk, current_identity)

    return jsonify(obj.topology_data())


@api.route('/clusters/<uuid:pk>/kubeconfig', methods=['GET'])
@jwt_required()
def cluster_kubeconfig(pk):
    obj = get_object(Cluster, pk, current_identity)

    return jsonify(obj.kubeconfig)


@api.route('/clusters/<uuid:pk>/progress', methods=['GET'])
@jwt_required()
def cluster_progress(pk):
    obj = get_object(Cluster, pk, current_identity)
    try:
        progress = obj.engine.get_progress()
    except NotImplementedError:
        progress = {
            'response': 501,
            'progress': 0,
            'result': obj.update_state()
        }
    except Exception:
        progress = {
            'response': 500,
            'progress': 0,
            'result': config.get('CLUSTER_UNKNOWN_STATE')
        }
    return jsonify(progress)


@api.route('/clusters/<uuid:pk>/resize', methods=['PATCH'])
@jwt_required()
def cluster_resize(pk):
    obj = get_object(Cluster, pk, current_identity)

    data = request.json
    if not isinstance(data, dict) or (isinstance(data, dict) and 'node_count' not in data):
        abort(400)

    res_status, res_msg = obj.engine.resize(data['node_count'])

    if not res_status:
        logger.error('Resizing failed: {}'.format(res_msg))
        abort(500, description=res_msg)

    # get object with updated metadata
    output = obj.engine.cluster
    return jsonify(output)


@api.route('/clusters/<uuid:pk>/set_network_policy', methods=['PATCH'])
@jwt_required()
def cluster_set_network_policy(pk):
    obj = get_object(Cluster, pk, current_identity)

    if not request.json:
        abort(400, description='JSON data expected')

    data = request.json
    if not all(k in data for k in ('provider', 'enabled')):
        msg = 'Incorrect network policy configuration {}'.format(data)
        logger.error(msg)
        abort(400, description=msg)

    res_status, res_msg = obj.engine.set_network_policy(data['provider'], data['enabled'])
    if not res_status:
        logger.error('Setting network policy failed: {}'.format(res_msg))
        abort(500, description=res_msg)

    # get object with updated metadata
    output = obj.engine.cluster
    return jsonify(output)


# Provisioners
class ListProvisioners(ListView):
    object_class = Provisioner

    supported_sort_fields = {
        'name': lambda x: (x.name, x.created_at, x.id),
        'engine': lambda x: (x.engine, x.name, x.id),
        'created_at': lambda x: (x.created_at, x.id),
        'created': lambda x: (x.created_at, x.id),
        'status': lambda x: (x.state, x.name, x.id),
        'state': lambda x: (x.state, x.name, x.id),
    }

    def filter_objects(self, objects, filters):
        if not filters:
            return objects

        get_provisioner_attr = {
            'name': lambda x: x.name,
            'engine': lambda x: x.engine,
        }
        filter_keys = set(filters.keys()).intersection(get_provisioner_attr.keys())
        return list(filter(
            lambda x: all(filters[key].lower() in get_provisioner_attr[key](x).lower()
                          for key in filter_keys),
            objects
        ))

    def sort_objects(self, objects, key, order):
        return sorted(objects, key=self.supported_sort_fields[key], reverse=order == 'asc')

    async def _update_provisioners(self, provisioners, loop):
        futures = [
            loop.run_in_executor(
                None,
                provisioner.engine_status
            )
            for provisioner in provisioners
        ]
        for result in await asyncio.gather(*futures):
            pass

    def get_content(self, *args, **kwargs):
        provisioners = self.obj
        if config.get('PROVISIONER_STATE_ON_LIST'):
            try:
                # get or establish event loop
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_closed():
                        raise RuntimeError('Loop already closed')
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                # run coroutines and close loop
                loop.run_until_complete(self._update_provisioners(provisioners, loop))
                loop.close()
            except Exception as e:
                logger.exception('Asyncio loop is NOT available, fallback to simple looping: ')

                for p in provisioners:
                    p.engine_status()
                self.obj = provisioners

        return super().get_content(self, *args, **kwargs)


class GetProvisionersHealth(ListView):
    object_class = Provisioner

    def get_content(self, *args, **kwargs):
        provisioners = self.obj

        healthy_provisioners = 0
        for provisioner in provisioners:
            if provisioner.state != config.get('PROVISIONER_ERROR_STATE'):
                healthy_provisioners += 1

        provisioner_health = 0
        if healthy_provisioners and provisioners:
            provisioner_health = int((healthy_provisioners / len(provisioners)) * 100)

        return {
            'total': len(provisioners),
            'healthy_percentage': provisioner_health
        }


class CreateProvisioner(CreateView):
    object_class = Provisioner


class GetProvisioner(GetView):
    object_class = Provisioner


class UpdateProvisioner(UpdateView):
    object_class = Provisioner


class DeleteProvisioner(DeleteView):
    object_class = Provisioner


api.add_url_rule('/provisioners', view_func=ListProvisioners.as_view('provisioner_list'))
api.add_url_rule('/provisioners/health',
                 view_func=GetProvisionersHealth.as_view('provisioners_health'))
api.add_url_rule('/provisioners', view_func=CreateProvisioner.as_view('provisioner_create'))
api.add_url_rule('/provisioners/<uuid:pk>', view_func=GetProvisioner.as_view('provisioner_get'))
api.add_url_rule('/provisioners/<uuid:pk>', view_func=UpdateProvisioner.as_view('provisioner_update'))
api.add_url_rule('/provisioners/<uuid:pk>', view_func=DeleteProvisioner.as_view('provisioner_delete'))


@api.route('/provisioners/engines', methods=['GET'])
@jwt_required()
def provisioner_engine_list():
    engine_cls = []
    module_path = 'kqueen.engines'

    for engine in Provisioner.list_engines():
        try:
            module = import_module(module_path)
            _class = getattr(module, engine)
            parameters = _class.get_parameter_schema()
            name = '.'.join([module_path, engine])
            engine_cls.append({
                'name': name,
                'verbose_name': getattr(_class, 'verbose_name', name),
                'parameters': parameters
            })
        except NotImplementedError:
            logger.exception('UI parameters is not set for engine: {}'.format(engine))
            engine_cls.append({
                'name': engine,
                'verbose_name': engine,
                'parameters': {
                    'provisioner': {},
                    'cluster': {}
                }
            })
        except Exception:
            logger.exception('Unable to read parameters for engine: {}'.format(engine))

    return jsonify(engine_cls)


# Organizations
class ListOrganizations(ListView):
    object_class = Organization


class CreateOrganization(CreateView):
    object_class = Organization


class GetOrganization(GetView):
    object_class = Organization


class UpdateOrganization(UpdateView):
    object_class = Organization


class DeleteOrganization(DeleteView):
    object_class = Organization


api.add_url_rule('/organizations', view_func=ListOrganizations.as_view('organization_list'))
api.add_url_rule('/organizations', view_func=CreateOrganization.as_view('organization_create'))
api.add_url_rule('/organizations/<uuid:pk>', view_func=GetOrganization.as_view('organization_get'))
api.add_url_rule('/organizations/<uuid:pk>', view_func=UpdateOrganization.as_view('organization_update'))
api.add_url_rule('/organizations/<uuid:pk>', view_func=DeleteOrganization.as_view('organization_delete'))


@api.route('/organizations/<uuid:pk>/policy', methods=['GET'])
@jwt_required()
def organization_policy(pk):
    obj = get_object(Organization, pk, current_identity)
    policies = config.get('DEFAULT_POLICIES', {})
    if hasattr(obj, 'policy') and obj.policy:
        policies.update(obj.policy)

    return jsonify(policies)


@api.route('/organizations/<uuid:pk>/deletable', methods=['GET'])
@jwt_required()
def organization_deletable(pk):
    obj = get_object(Organization, pk, current_identity)
    deletable, remaining = obj.is_deletable()
    response = {
        'deletable': deletable,
        'remaining': remaining
    }
    return jsonify(response)


# Users
class ListUsers(ListView):
    object_class = User


class CreateUser(CreateView):
    object_class = User


class GetUser(GetView):
    object_class = User


class UpdateUser(UpdateView):
    object_class = User

    def dispatch_request(self, *args, **kwargs):
        self.check_authentication()

        if not request.json:
            abort(400, description='JSON data expected')

        data = request.json
        if not isinstance(data, dict):
            abort(400)

        self.set_object(*args, **kwargs)

        if 'password' in data:
            del data['password']

        for key, value in data.items():
            setattr(self.obj, key, value)

        try:
            self.obj.save()
        except Exception:
            abort(500)

        output = self.get_content(*args, **kwargs)
        return jsonify(output)


class DeleteUser(DeleteView):
    object_class = User

    def get_policy_key(self):
        policy_key = super().get_policy_key()
        return '{}_{}'.format(policy_key, self.obj.role)


api.add_url_rule('/users', view_func=ListUsers.as_view('user_list'))
api.add_url_rule('/users', view_func=CreateUser.as_view('user_create'))
api.add_url_rule('/users/<uuid:pk>', view_func=GetUser.as_view('user_get'))
api.add_url_rule('/users/<uuid:pk>', view_func=UpdateUser.as_view('user_update'))
api.add_url_rule('/users/<uuid:pk>', view_func=DeleteUser.as_view('user_delete'))


@api.route('/users/<uuid:pk>/updatepw', methods=['PATCH'])
@jwt_required()
def user_password_update(pk):
    obj = get_object(User, pk, current_identity)

    if not request.json:
        abort(400, description='JSON data expected')

    data = request.json
    if not isinstance(data, dict):
        abort(400)

    obj.password = encrypt_password(data.get('password'))

    try:
        obj.save()
    except Exception:
        abort(500)

    return jsonify(obj)


@api.route('/users/whoami', methods=['GET'])
@jwt_required()
def user_whoami():
    output = current_identity

    return jsonify(output)


@api.route('/swagger', methods=['GET'])
def swagger_json():
    try:
        base_path = os.path.abspath(os.path.dirname(__file__))
        file_path = os.path.join(base_path, 'api.yml')
        with open(file_path, 'r') as f:
            _yaml = f.read()
        data = yaml.safe_load(_yaml)
    except FileNotFoundError:
        logger.exception('Swagger YAML not found on {}.'.format(file_path))
        abort(404)
    except Exception as e:
        logger.exception('Error')
        abort(500)

    return jsonify(data)


@api.route('/configurations/auth', methods=['GET'])
@jwt_required()
def auth_params_configuration():

    auth_opts = generate_auth_options(config.get("AUTH_MODULES"))
    try:
        for name, configuration in auth_opts.items():
            auth_cls_name = configuration['engine']
            module = import_module('kqueen.auth')
            _class = getattr(module, auth_cls_name)

            # Add UI fields description, verbose name and hide secure parameters
            secure_params = {}
            for k, v in configuration['parameters'].items():
                if k.startswith('_'):
                    v = '*****'
                secure_params[k] = v

            auth_opts[name].update(
                {'ui_parameters': _class.get_parameter_schema(),
                 'name': getattr(_class, 'verbose_name', name),
                 'parameters': secure_params})

    except NotImplementedError:
        logger.exception('UI parameters is not specified for "{}" auth type'.format(name))
    except Exception:
        logger.exception('Unable to read UI parameters for "{}" auth type'.format(name))
    return jsonify(auth_opts)
