# PoseMesh Hide

**Selección y ocultación de geometría de una malla en Blender, sin salir de Pose Mode ni separar objetos.**

PoseMesh Hide es un addon para trabajar sobre vértices, aristas y caras de una malla mientras se manipula su armature. Añade un selector de componentes para **Pose Mode** y **Object Mode**, utiliza la selección nativa en **Edit Mode** y permite controlar en qué modos se aplica la ocultación.

## Características

- Selección de **vértices, aristas y caras** desde Pose Mode u Object Mode mediante un selector propio dentro del visor 3D.
- Ocultación **reversible y acumulativa** sin separar la malla, modificar su topología original ni alterar pesos de vértices o relaciones con huesos.
- Acciones **Hide**, **Isolate**, **Invert** y **Reveal all**.
- Activación por modo mediante casillas **Object / Pose / Edit**, combinables con lógica **OR**.
- Atajos de **doble pulsación de H** para reducir conflictos con las acciones normales de Blender.
- Controles de visualización **X-Ray**, **Wireframe** y **Select Through**.
- Panel de diagnóstico **Print Event API** para inspeccionar los eventos recibidos por Blender.

## Requisitos

- **Blender 5.2.x** (objetivo: **5.2.3**).
- No requiere librerías de Python externas.
- Archivo del addon: `posemesh_hide.py`.

No se ha verificado su funcionamiento en otras ramas de Blender.

## Instalación

1. Descarga `posemesh_hide.py`.
2. En Blender, entra en **Edit → Preferences → Add-ons → Install from Disk**.
3. Selecciona el archivo `.py` y activa **PoseMesh Hide — Geometry Selection Across Modes**.
4. Abre el **3D Viewport**, pulsa `N` y entra en la pestaña **PoseMesh**.

Para actualizar desde la versión 0.1.0, desactiva el addon antiguo, instala el archivo nuevo y reinicia Blender. La versión 0.1.1 conserva los atributos de malla y grupos de Geometry Nodes creados por la versión anterior.

## Inicio rápido

1. Selecciona tu armature y entra en **Pose Mode**.
2. En **N → PoseMesh**, asigna la malla que quieres manipular en **Target Mesh**. Es recomendable seleccionarla expresamente, especialmente cuando hay varias mallas asociadas al rig.
3. Marca los modos donde quieres aplicar el efecto: `Pose`, `Object`, `Edit` o cualquier combinación.
4. Pulsa **Select Mesh Components**.
5. Selecciona **Vertex**, **Edge** o **Face** y haz clic en los elementos deseados.
6. Ejecuta **Hide** desde el panel o pulsa `H` dos veces consecutivas.
7. Usa **Reveal all** o `Alt + H`, `Alt + H` para recuperar la geometría oculta.

El selector permite trabajar sobre la malla sin cambiar a Edit Mode. Para terminar la herramienta de selección, pulsa `Esc` o clic derecho.

### Selección dentro del picker

| Entrada | Acción |
| --- | --- |
| Clic izquierdo | Seleccionar componente |
| `Shift` + clic | Añadir o alternar selección |
| `Ctrl` + clic | Retirar de la selección |
| `1` | Seleccionar vértices |
| `2` | Seleccionar aristas |
| `3` | Seleccionar caras |
| `A` | Seleccionar todos los elementos visibles |
| `Alt + A` | Deseleccionar |
| `B` + arrastrar | Selección rectangular |
| `Esc` o clic derecho | Salir del picker |

En **Edit Mode** se utiliza la selección nativa de Blender; no es necesario activar el picker.

## Atajos de visibilidad

Pulsa la combinación dos veces dentro de una ventana de **330 ms**. Mantén el modificador (`Shift`, `Ctrl` o `Alt`) en las dos pulsaciones.

| Atajo | Acción de PoseMesh Hide |
| --- | --- |
| `H`, `H` | Ocultar selección (**Hide**) |
| `Shift + H`, `Shift + H` | Mostrar solo lo seleccionado (**Isolate**) |
| `Ctrl + H`, `Ctrl + H` | Invertir visibilidad (**Invert**) |
| `Alt + H`, `Alt + H` | Revelar toda la geometría oculta (**Reveal all**) |

Una sola pulsación intenta mantener la acción nativa de Blender tras el intervalo de detección (~330 ms). La compatibilidad con atajos personalizados puede variar; en particular, la recuperación de una acción nativa de `Ctrl + H` depende del contexto y de los keymaps activos.

Los atajos del addon solo deben actuar si el **modo actual está habilitado** en el panel.

## Combinación de modos (OR)

Las casillas **Object**, **Pose** y **Edit** son independientes. El efecto se habilita en cada modo marcado:

| Modos habilitados | Resultado |
| --- | --- |
| Solo `Pose` | La geometría se oculta en Pose Mode; fuera de ese modo la vista filtrada se desactiva. |
| `Pose` + `Object` | La misma ocultación se visualiza en ambos modos. |
| `Object` + `Pose` + `Edit` | La vista filtrada se habilita en los tres modos. |

**Importante:** los modos no disponen de máscaras de ocultación separadas. Comparten **una sola máscara por malla**; las casillas controlan **dónde se aplica**, no diferentes conjuntos de caras ocultas por modo. La conmutación de visibilidad puede actualizarse con un pequeño retraso al cambiar de modo.

## X-Ray y Wireframe

Desde el panel **PoseMesh**:

- **X-Ray** activa o desactiva la transparencia del visor.
- **Wireframe** alterna entre la visualización sólida y la de alambre.
- **Transparency** controla la opacidad de X-Ray.
- **Select Through** permite seleccionar componentes proyectados detrás de la superficie visible.

Estas opciones cambian la visualización y el comportamiento de selección del visor; no alteran los materiales de la malla.

## Cómo funciona

El addon crea tres atributos enteros de visibilidad en la malla y un **Geometry Nodes Modifier** denominado `PoseMesh Hide (preview only)`.

| Atributo | Dominio | Uso |
| --- | --- | --- |
| `_pmh_hide_v` | Point | Ocultación de vértices |
| `_pmh_hide_e` | Edge | Ocultación de aristas |
| `_pmh_hide_f` | Face | Ocultación de caras |

El grupo de Geometry Nodes conserva referencias a índices originales de los componentes para relacionar la geometría evaluada con la malla de origen. El modificador se sitúa al final de la pila para que la selección pueda trabajar sobre el resultado deformado por el armature.

**No se eliminan vértices, aristas ni caras de la malla fuente.** La geometría se filtra en la vista evaluada. Por defecto, el modificador tiene desactivada su visibilidad de render.

### Limitaciones conocidas

- **No anima la visibilidad mediante keyframes.** Su propósito actual es facilitar la edición y el posing, no generar una animación de aparición/desaparición exportable a un motor de juego.
- El picker implementa clic, suma/resta y selección rectangular; **no** sustituye todas las herramientas de Edit Mode (loop select, lasso, etc.).
- Modificadores anteriores que **cambien la topología** (por ejemplo, Remesh o Geometry Nodes que regenere la malla) pueden invalidar la correspondencia de índices usada por el selector.
- En Edit Mode, las operaciones del addon también interactúan con los indicadores de ocultación nativos de BMesh. **Reveal all** puede alterar una ocultación manual previa.
- No admite mallas vinculadas en **solo lectura**. Si una malla comparte sus datos con otros objetos, el addon puede crear una copia de sus datos para evitar modificar las otras instancias.
- Antes de exportar a **FBX, glTF, OBJ** u otros formatos, comprueba la configuración de **Apply Modifiers** y desactiva el modificador de vista previa si no deseas exportar geometría filtrada.
- Los atajos pueden entrar en conflicto con keymaps personalizados.

## Diagnóstico

El panel incluye **Print Event API** para investigar cambios de API o problemas con los eventos del teclado/ratón.

1. En Windows, abre **Window → Toggle System Console**.
2. Abre la pestaña **PoseMesh**.
3. Pulsa **Print Event API**.
4. Revisa en la consola los campos, métodos públicos y propiedades RNA del evento capturado.

Si encuentras un error, abre un **GitHub Issue** con la versión de Blender, los pasos para reproducirlo, el traceback completo de la consola y, si es posible, un `.blend` mínimo que reproduzca el problema.

## Desinstalación y limpieza

Desactivar o desinstalar el addon **no elimina automáticamente** los atributos y modificadores añadidos a tus archivos `.blend`.

Para retirar la vista filtrada de una malla, elimina manualmente su modificador **PoseMesh Hide (preview only)**. Si quieres eliminar también los metadatos del addon, borra los atributos `_pmh_hide_v`, `_pmh_hide_e` y `_pmh_hide_f` desde las propiedades de datos de la malla, después de comprobar que ya no los necesitas.
