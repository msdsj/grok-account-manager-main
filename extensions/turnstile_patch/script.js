(function patchMouseCoordinates() {
    function getRandomInt(min, max) {
        return Math.floor(Math.random() * (max - min + 1)) + min;
    }

    function defineCoordinate(name, value) {
        const current = Object.getOwnPropertyDescriptor(MouseEvent.prototype, name);
        if (current && current.configurable === false) {
            return;
        }
        Object.defineProperty(MouseEvent.prototype, name, {
            configurable: true,
            enumerable: current ? current.enumerable : true,
            get: function () { return value; },
        });
    }

    defineCoordinate('screenX', getRandomInt(800, 1200));
    defineCoordinate('screenY', getRandomInt(400, 700));
})();
