import hmac
from functools import wraps
from typing import Any, Dict, List, Tuple, Union

import orjson
import shortuuid
from django.conf import settings
from django.db.models import Model
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from .components import UnicornField, UnicornView


class UnicornViewError(Exception):
    pass


def handle_error(view_func):
    def wrapped_view(*args, **kwargs):
        try:
            return view_func(*args, **kwargs)
        except UnicornViewError as e:
            return JsonResponse({"error": str(e)})
        except AssertionError as e:
            return JsonResponse({"error": str(e)})

    return wraps(view_func)(wrapped_view)


def _set_property_from_data(
    component_or_field: Union[UnicornView, UnicornField, Model], name: str, value
) -> None:
    """
    Sets properties on the component based on passed-in data.
    """
    if hasattr(component_or_field, name):
        field = getattr(component_or_field, name)

        # UnicornField and Models are always a dictionary (can be nested)
        if isinstance(field, UnicornField) or isinstance(field, Model):
            for key in value.keys():
                key_value = value[key]
                _set_property_from_data(field, key, key_value)
        else:
            setattr(component_or_field, name, value)


def _set_property_from_payload(
    component: UnicornView, payload: Dict, data: Dict
) -> None:
    """
    Sets properties on the component based on the payload.
    Also updates the data dictionary which gets set back as part of the payload.

    Args:
        param component: Component to set attributes on.
        param payload: Dictionary that comes with request.
        param data: Dictionary that gets sent back with the response.
    """

    property_name = payload.get("name")
    property_value = payload.get("value")

    if property_name is not None and property_value is not None:
        """
        Handles nested properties. For example, for the following component:

        class Author(UnicornField):
            name = "Neil"

        class TestView(UnicornView):
            author = Author()
        
        `payload` would equal `{'name': 'author.name', 'value': 'Neil Gaiman'}`

        The following code updates UnicornView.author.name based the payload's `author.name`.
        """
        property_name_parts = property_name.split(".")
        component_or_field = component
        data_or_dict = data  # Could be an internal portion of data that gets set

        for (idx, property_name_part) in enumerate(property_name_parts):
            if hasattr(component_or_field, property_name_part):
                if idx == len(property_name_parts) - 1:
                    setattr(component_or_field, property_name_part, property_value)
                    data_or_dict[property_name_part] = property_value
                else:
                    component_or_field = getattr(component_or_field, property_name_part)
                    data_or_dict = data_or_dict.get(property_name_part, {})
            elif isinstance(component_or_field, dict):
                if idx == len(property_name_parts) - 1:
                    component_or_field[property_name_part] = property_value
                    data_or_dict[property_name_part] = property_value
                else:
                    component_or_field = component_or_field[property_name_part]
                    data_or_dict = data_or_dict.get(property_name_part, {})


def _parse_call_method_name(call_method_name: str) -> Tuple[str, List[Any]]:
    """
    Parses the method name from the request payload into a set of parameters to pass to a method.

    Args:
        param call_method_name: String representation of a method name with parameters, e.g. "set_name('Bob')"

    Returns:
        Tuple of method_name and a list of arguments.
    """

    method_name = call_method_name
    params: List[Any] = []

    if "(" in call_method_name and call_method_name.endswith(")"):
        param_idx = call_method_name.index("(")
        params_str = call_method_name[param_idx:]

        # Remove the arguments from the method name
        method_name = call_method_name.replace(params_str, "")

        # Remove parenthesis
        params_str = params_str[1:-1]

        if params_str == "":
            return (method_name, params)

        # Split up mutiple args
        params = params_str.split(",")

        for idx, arg in enumerate(params):
            params[idx] = _handle_arg(arg)

        # TODO: Handle kwargs

    return (method_name, params)


def _handle_arg(arg):
    """
    Clean up arguments. Mostly used to handle strings.

    Returns:
        Cleaned up argument.
    """
    if (arg.startswith("'") and arg.endswith("'")) or (
        arg.startswith('"') and arg.endswith('"')
    ):
        return arg[1:-1]


def _call_method_name(
    component: UnicornView, method_name: str, params: List[Any], data: Dict
) -> None:
    """
    Calls the method name with parameters.
    Also updates the data dictionary which gets set back as part of the payload.

    Args:
        param component: Component to call method on.
        param method_name: Method name to call.
        param params: List of arguments for the method.
        param data: Dictionary that gets sent back with the response.
    """

    if method_name is not None and hasattr(component, method_name):
        func = getattr(component, method_name)

        if params:
            func(*params)
        else:
            func()

        # Re-set all attributes because they could have changed after the method call
        for (attribute_name, attribute_value,) in component._attributes().items():
            data[attribute_name] = attribute_value


class ComponentRequest:
    """
    Parses, validates, and stores all of the data from the message request.
    """

    def __init__(self, request):
        self.body = {}

        try:
            self.body = orjson.loads(request.body)
            assert self.body, "Invalid JSON body"
        except orjson.JSONDecodeError as e:
            raise UnicornViewError("Body could not be parsed") from e

        self.data = self.body.get("data")
        assert self.data is not None, "Missing data"  # data could theoretically be {}

        self.id = self.body.get("id")
        assert self.id, "Missing component id"

        self.validate_checksum()

        self.action_queue = self.body.get("actionQueue", [])

    def validate_checksum(self):
        """
        Validates that the checksum in the request matches the data.

        Returns:
            Raises `AssertionError` if the checksums don't match.
        """
        checksum = self.body.get("checksum")
        assert checksum, "Missing checksum"

        generated_checksum = hmac.new(
            str.encode(settings.SECRET_KEY),
            orjson.dumps(self.data),
            digestmod="sha256",
        ).hexdigest()
        generated_checksum = shortuuid.uuid(generated_checksum)[:8]
        assert checksum == generated_checksum, "Checksum does not match"


@handle_error
@csrf_protect
@require_POST
def message(request: HttpRequest, component_name: str = None) -> JsonResponse:
    """
    Endpoint that instantiates the component and does the correct action
    (set an attribute or call a method) depending on the JSON payload in the body.

    Args:
        param request: HttpRequest for the function-based view.
        param: component_name: Name of the component, e.g. "hello-world".
    
    Returns:
        JSON with the following structure:
        {
            "id": component_id,
            "dom": html,  // re-rendered version of the component after actions in the payload are completed
            "data": {},  // updated data after actions in the payload are completed
        }
    """

    assert component_name, "Missing component name in url"

    component_request = ComponentRequest(request)
    component = UnicornView.create(
        component_id=component_request.id, component_name=component_name
    )

    # Set component properties based on request data
    for (name, value) in component_request.data.items():
        _set_property_from_data(component, name, value)

    for action in component_request.action_queue:
        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "syncInput":
            _set_property_from_payload(component, payload, component_request.data)
        elif action_type == "callMethod":
            call_method_name = payload.get("name", "")
            assert call_method_name, "Missing 'name' key for callMethod"

            # Handle the special case of the reset action
            if call_method_name == "reset" or call_method_name == "reset()":
                component = UnicornView.create(
                    component_id=component_request.id,
                    component_name=component_name,
                    skip_cache=True,
                )
                # Reset the data based on component's attributes
                component_request.data = component._attributes()
            elif "=" in call_method_name:
                call_method_name_split = call_method_name.split("=")
                property_name = call_method_name_split[0]
                property_value = _handle_arg(call_method_name_split[1])

                if hasattr(component, property_name):
                    setattr(component, property_name, property_value)
                    component_request.data[property_name] = property_value
            else:
                (method_name, params) = _parse_call_method_name(call_method_name)
                _call_method_name(
                    component, method_name, params, component_request.data
                )
        else:
            raise UnicornViewError(f"Unknown action_type '{action_type}'")

    rendered_component = component.render()

    res = {
        "id": component_request.id,
        "dom": rendered_component,
        "data": component_request.data,
    }

    return JsonResponse(res)
