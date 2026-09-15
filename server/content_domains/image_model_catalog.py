"""Image model identifiers shared by runtime routing and operator views."""
import os


OPENAI_IMAGE_MODEL = 'gpt-image-2'
XIAOLE_IMAGE_MODEL = 'gpt-image-2'
SEEDREAM_MODELS = {
    'std': os.environ.get('ARK_SEEDREAM_MODEL', 'doubao-seedream-5-0-260128'),
    'pro': os.environ.get('ARK_SEEDREAM_PRO_MODEL', 'doubao-seedream-5-0-pro-260628'),
}
