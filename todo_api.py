import api_utils

api = api_utils.API()

@api.GET("/api/")
def default(request):
    return "Willkommen zum Todo-API"

todos = [
    {
        "text":"Abwaschen",
        "done": False,
    },
    {
        "text":"Aufräumen",
        "done": False,
    },
    {
        "text":"Kette ölen (Velo)",
        "done": True,
    },
]  # "Datenbank"

@api.GET("/api/todos/")
def list_todos(request):
    return todos

@api.POST("/api/todos/")
def new_entry(request, text:str):
    todos.append({"text": text, "done": False})

@api.PUT("/api/todos/<int:index>")
def update_entry(request, index, done:bool):
    if index >= len(todos):
        raise api_utils.NotFound()
    todos[index]["done"] = done

@api.DELETE("/api/todos/<int:index>")
def delete_entry(request, index):
    if index >= len(todos):
        raise api_utils.NotFound()
    del todos[index]

api_utils.run(api, hostname="127.0.0.1", port=5000)