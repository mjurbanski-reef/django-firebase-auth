import importlib
from typing import Optional

from firebase_admin import credentials, auth, initialize_app
from firebase_admin.auth import ExpiredIdTokenError
from firebase_admin.exceptions import FirebaseError
from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.http.request import HttpHeaders, QueryDict
from django.contrib.auth import login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.models import AbstractBaseUser
from django.template import loader
from django.shortcuts import redirect, resolve_url
from django.urls import reverse
from django.urls.exceptions import NoReverseMatch
from django.views import View

from django_firebase_auth.conf import AUTH_BACKEND, SERVICE_ACCOUNT_FILE, WEB_API_KEY, AUTH_DOMAIN, JWT_HEADER_NAME, \
    ALLOW_NOT_CONFIRMED_EMAILS, ENABLE_GOOGLE_LOGIN, ADMIN_LOGIN_REDIRECT_URL, GET_OR_CREATE_USER_CLASS, \
    CREATE_USER_IF_NOT_EXISTS
from django_firebase_auth.user_getter import AbstractUserGetter, UserNotFound

GET_OR_CREATE_USER_MODULE, GET_OR_CREATE_USER_CLASS_NAME = GET_OR_CREATE_USER_CLASS.split(':')
GET_OR_CREATE_USER_CLASS = getattr(importlib.import_module(GET_OR_CREATE_USER_MODULE), GET_OR_CREATE_USER_CLASS_NAME)
user_getter: AbstractUserGetter = GET_OR_CREATE_USER_CLASS(CREATE_USER_IF_NOT_EXISTS)

if SERVICE_ACCOUNT_FILE:
    firebase_credentials = credentials.Certificate(SERVICE_ACCOUNT_FILE)
    initialize_app(firebase_credentials)


class AuthError(Exception):
    error_type = 'OTHER'
    error_description = 'Something went wrong'

    @classmethod
    def make_response_body(cls):
        return {'error': cls.error_type, 'description': cls.error_description}


class NoAuthHeader(AuthError):
    error_type = 'NO_AUTH_HEADER'
    error_description = 'Missing Firebase authentication header'


class JWTExpired(AuthError):
    error_type = 'JWT_EXPIRED'
    error_description = 'Firebase authentication token is expired'


class JWTInvalid(AuthError):
    error_type = 'JWT_INVALID'
    error_description = 'Firebase authentication token is invalid'


class UserNotRegistered(AuthError):
    error_type = 'USER_NOT_REGISTERED'
    error_description = 'This user has not been registered'


class EmailNotVerified(AuthError):
    error_type = 'EMAIL_NOT_VERIFIED'
    error_description = 'User email has not been verified'


def authenticate(request: HttpRequest):
    try:
        jwt_payload = _verify_firebase_account(request.headers)
    except AuthError as ex:
        return JsonResponse(ex.make_response_body(), status=401)

    try:
        user = user_getter.get_or_create_user(jwt_payload)
    except UserNotFound:
        return JsonResponse(UserNotRegistered.make_response_body(), status=401)

    login(request=request, user=user, backend=AUTH_BACKEND)
    return JsonResponse({"status": "ok"})


def logout(request: HttpRequest):
    django_logout(request)
    return JsonResponse({"status": "ok"})


def _verify_firebase_account(headers: HttpHeaders) -> dict:
    jwt = headers.get(JWT_HEADER_NAME)
    if jwt is None:
        raise NoAuthHeader()
    try:
        decoded_token = auth.verify_id_token(jwt)
    except ExpiredIdTokenError:
        raise JWTExpired()
    except FirebaseError:
        raise JWTInvalid()

    is_email_verified = decoded_token["email_verified"]
    if not is_email_verified and not ALLOW_NOT_CONFIRMED_EMAILS:
        raise EmailNotVerified()

    return decoded_token


class AdminLoginView(View):

    _BAD_CREDENTIALS = "Wrong email or password"
    _NON_STAFF = "To access admin panel, you must login as a staff member"

    def _render(self, request: HttpRequest, next: str, error: Optional[str]=None) -> HttpResponse:
        template = loader.get_template('firebase_authentication/login.html')
        return HttpResponse(
            template.render({
                'firebase_web_api_key': WEB_API_KEY,
                'firebase_auth_domain': AUTH_DOMAIN,
                'enable_google_login': ENABLE_GOOGLE_LOGIN,
                'jwt_header_name': JWT_HEADER_NAME,
                'firebase_auth_endpoint': reverse(authenticate),
                'login_redirect_url': next,
                'error': error,
            },
            request)
        )

    def _get_next(self, query: QueryDict) -> str:
        next = query.get('next')
        if next:
            try:
                next = resolve_url(next)
            except NoReverseMatch:
                pass
        return next or resolve_url(ADMIN_LOGIN_REDIRECT_URL)

    def get(self, request: HttpRequest) -> HttpResponse:
        next = self._get_next(request.GET)
        if request.user.is_authenticated:
            if request.user.is_staff:
                return redirect(next)
            else:
                return self._render(request, next, self._NON_STAFF)
        return self._render(request, next)

    def post(self, request: HttpRequest) -> HttpResponse:
        """Login with Django credentials.
        This view expect email and password in POST form."""

        next = self._get_next(request.POST)

        email = request.POST.get("email")
        if not email:
            return self._render(request, next, "Email field must be non-empty")
        
        password = request.POST.get("password")
        if not password:
            return self._render(request, next, "Password field must be non-empty")

        UserModel = get_user_model()
        user: AbstractBaseUser
        try:
            user = UserModel.objects.get(email=email)
        except UserModel.DoesNotExist:
            return self._render(request, next, self._BAD_CREDENTIALS)

        if user.check_password(password):
            login(request, user=user, backend=AUTH_BACKEND)
            if request.user.is_staff:
                return redirect(next)
            else:
                return self._render(request, next, self._NON_STAFF)
        else:
            return self._render(request, next, self._BAD_CREDENTIALS)
