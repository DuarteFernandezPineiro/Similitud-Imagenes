# V1 funcional — agrupador de imágenes similares

Esta primera versión recorre una carpeta y todas sus subcarpetas, ignora los
vídeos (solo intenta abrir extensiones de imagen) y genera un JSON con los
grupos encontrados. No mueve, copia ni borra ningún archivo. Admite los
formatos habituales y también HEIC/HEIF, frecuentes en fotos de iPhone.

La similitud es **perceptual**, no por nombre ni tamaño: una misma foto
recomprimida, redimensionada o con leves cambios normalmente conserva un hash
parecido. El valor de 100% significa que el hash perceptual es idéntico; no es
una comprobación de que los bytes del archivo sean iguales.

## Instalación

En PowerShell, desde esta carpeta:

```powershell
python -m pip install -r requirements.txt
```

## Uso

```powershell
python agrupar_imagenes.py "C:\\Fotos" --similitud 90 --salida "C:\\Fotos\\grupos_similares.json"
```

Los únicos valores permitidos para `--similitud` son `80`, `90`, `95` y `100`.
Si se omite, se usa `90`.

Para limitar el uso de CPU, por ejemplo a cuatro procesos:

```powershell
python agrupar_imagenes.py "C:\\Fotos" --similitud 95 --procesos 4
```

## Ejecuciones posteriores

En la primera ejecución se crea automáticamente el archivo oculto
`.similitud_imagenes.sqlite` dentro de la carpeta analizada. Contiene la ruta,
el tamaño, la fecha de modificación y el pHash de cada imagen.

En las siguientes ejecuciones el programa recorre las rutas para detectar
cambios, pero solo vuelve a calcular el hash de las imágenes nuevas o cuyos
tamaño o fecha de modificación hayan cambiado. También elimina de la caché las
entradas de los archivos que ya no existen. El JSON indicará cuántos hashes se
reutilizaron y cuántos se calcularon en esa ejecución.

Para imágenes **nuevas**, SQLite busca sus candidatas en el índice persistente
y actualiza únicamente los grupos a los que se conecten. No vuelve a comparar
ni a reconstruir los grupos ya existentes. Si se modifica o borra una imagen,
o se cambia el porcentaje de similitud, se reconstruyen todos los grupos: es
necesario porque un grupo podría dividirse.

Se puede guardar la caché en otra ubicación con `--cache`:

```powershell
python agrupar_imagenes.py "C:\\Fotos" --similitud 90 --cache "D:\\datos\\fotos-cache.sqlite"
```

## Rendimiento

Primero se calcula un pHash de 64 bits de cada imagen en paralelo. Después se
usa un índice por fragmentos del hash para obtener únicamente candidatas y se
verifica la distancia exacta antes de unirlas. Así se evita comparar cada foto
contra todas las demás, algo inviable con 100.000 imágenes. Las imágenes que
conectan entre sí por encima del umbral se incluyen en el mismo grupo.
