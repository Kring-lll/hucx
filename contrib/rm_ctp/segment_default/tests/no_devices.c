#include <urma_api.h>
urma_device_t **urma_get_device_list(int *count)
{
    *count = 0;
    return NULL;
}
void urma_free_device_list(urma_device_t **list)
{
    (void)list;
}
urma_status_t urma_uninit(void)
{
    return URMA_SUCCESS;
}
